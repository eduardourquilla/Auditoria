from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import pandas as pd
import time
import re
import os
import json
import uuid
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from io import BytesIO
from datetime import datetime

app = FastAPI(title="Servicio de Auditoría DTE")

# Configuración CORS para permitir solicitudes desde el frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # En producción, limitar a tu dominio en Bold.new/Netlify
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Almacenamiento en memoria para los trabajos activos
active_jobs = {}
job_results = {}

class JobStatus(BaseModel):
    id: str
    status: str  # "running", "paused", "completed", "error"
    progress: int
    total: int
    results: List[Dict[str, Any]]

def setup_selenium():
    """Configura y retorna un driver de Selenium para entornos sin GUI"""
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    
    # Para servidores como Render, Railway, etc.
    if os.environ.get('IS_PRODUCTION'):
        # Usar chromedriver instalado en el sistema
        driver = webdriver.Chrome(options=chrome_options)
    else:
        # Para desarrollo local
        driver_path = "./chromedriver"  # Ajusta según tu entorno
        service = Service(driver_path)
        driver = webdriver.Chrome(service=service, options=chrome_options)
    
    return driver

def process_excel_file(job_id: str, file_content: bytes):
    """Procesa el archivo Excel y ejecuta la auditoría"""
    try:
        # Inicializar el trabajo
        active_jobs[job_id] = {"status": "running", "paused": False}
        job_results[job_id] = []
        
        # Leer archivo Excel
        df = pd.read_excel(BytesIO(file_content))
        df['Fecha'] = pd.to_datetime(df['Fecha'], errors='coerce')
        total_rows = len(df)
        
        # Configurar Selenium
        driver = setup_selenium()
        driver.maximize_window()
        
        try:
            # Procesar cada fila
            for index, row in df.iterrows():
                # Verificar si se debe detener el proceso
                if active_jobs[job_id]["status"] != "running":
                    break
                
                # Verificar si está pausado
                while active_jobs[job_id].get("paused", False):
                    time.sleep(1)
                
                # Extraer datos
                fecha = row['Fecha']
                codigo_generacion = str(row['Código de Generación']).strip()
                fecha_str = fecha.strftime("%d/%m/%Y") if pd.notnull(fecha) else ""
                
                try:
                    # Navegar al sitio
                    driver.get("https://admin.factura.gob.sv/consultaPublica")
                    wait = WebDriverWait(driver, 10)
                    
                    # Completar formulario
                    campo_fecha = wait.until(EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "input[formcontrolname='fechaEmi']")))
                    campo_fecha.clear()
                    campo_fecha.send_keys(fecha_str)
                    
                    campo_codigo = wait.until(EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "input[formcontrolname='codigoGeneracion']")))
                    campo_codigo.clear()
                    campo_codigo.send_keys(codigo_generacion)
                    
                    boton_buscar = wait.until(EC.element_to_be_clickable(
                        (By.XPATH, "//button[contains(text(), 'Realizar Búsqueda')]")))
                    boton_buscar.click()
                    
                    # Procesar resultado
                    try:
                        boton_error = WebDriverWait(driver, 3).until(
                            EC.element_to_be_clickable((By.XPATH, "//button[contains(@class, 'swal2-confirm') and contains(text(), 'Aceptar')]")))
                        boton_error.click()
                        monto_convertido = "Error"
                        auditoria_exitosa = "No"
                    except TimeoutException:
                        try:
                            label_monto = wait.until(EC.presence_of_element_located((By.XPATH, "//label[contains(text(), '$')]")))
                            monto_numerico = re.sub(r"[^\d.]", "", label_monto.text)
                            monto_convertido = float(monto_numerico) if monto_numerico else "No encontrado"
                            auditoria_exitosa = "Sí" if isinstance(monto_convertido, float) else "No"
                        except (TimeoutException, NoSuchElementException):
                            monto_convertido = "No encontrado"
                            auditoria_exitosa = "No"
                            
                except Exception as e:
                    monto_convertido = f"Error: {str(e)}"
                    auditoria_exitosa = "No"
                
                # Guardar resultado
                result = {
                    "fecha": fecha_str,
                    "codigo": codigo_generacion,
                    "monto": monto_convertido,
                    "estado": auditoria_exitosa
                }
                job_results[job_id].append(result)
                
                # Actualizar progreso
                active_jobs[job_id]["progress"] = index + 1
                active_jobs[job_id]["total"] = total_rows
            
            # Marcar como completado
            active_jobs[job_id]["status"] = "completed"
        
        finally:
            # Cerrar driver
            driver.quit()
    
    except Exception as e:
        # Manejar errores
        active_jobs[job_id]["status"] = "error"
        active_jobs[job_id]["error"] = str(e)

@app.post("/upload/", response_model=dict)
async def upload_file(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    """Recibe un archivo Excel y comienza el proceso de auditoría"""
    try:
        # Validar archivo
        if not file.filename.endswith(('.xlsx', '.xls')):
            raise HTTPException(status_code=400, detail="Solo se aceptan archivos Excel (.xlsx, .xls)")
        
        # Leer contenido
        file_content = await file.read()
        
        # Crear ID único para el trabajo
        job_id = str(uuid.uuid4())
        
        # Iniciar proceso en segundo plano
        background_tasks.add_task(process_excel_file, job_id, file_content)
        
        return {"jobId": job_id, "message": "Proceso iniciado correctamente"}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al procesar el archivo: {str(e)}")

@app.get("/job/{job_id}", response_model=JobStatus)
async def get_job_status(job_id: str):
    """Obtiene el estado actual de un trabajo"""
    if job_id not in active_jobs:
        raise HTTPException(status_code=404, detail="Trabajo no encontrado")
    
    job = active_jobs[job_id]
    results = job_results.get(job_id, [])
    
    return JobStatus(
        id=job_id,
        status=job["status"],
        progress=job.get("progress", 0),
        total=job.get("total", 0),
        results=results
    )

@app.post("/job/{job_id}/pause")
async def pause_job(job_id: str):
    """Pausa un trabajo en ejecución"""
    if job_id not in active_jobs:
        raise HTTPException(status_code=404, detail="Trabajo no encontrado")
    
    active_jobs[job_id]["paused"] = True
    return {"status": "paused"}

@app.post("/job/{job_id}/resume")
async def resume_job(job_id: str):
    """Reanuda un trabajo pausado"""
    if job_id not in active_jobs:
        raise HTTPException(status_code=404, detail="Trabajo no encontrado")
    
    active_jobs[job_id]["paused"] = False
    return {"status": "running"}

@app.post("/job/{job_id}/stop")
async def stop_job(job_id: str):
    """Detiene un trabajo en ejecución"""
    if job_id not in active_jobs:
        raise HTTPException(status_code=404, detail="Trabajo no encontrado")
    
    active_jobs[job_id]["status"] = "stopped"
    return {"status": "stopped"}

@app.get("/job/{job_id}/download")
async def download_results(job_id: str):
    """Retorna los resultados en formato JSON para descargar"""
    if job_id not in job_results:
        raise HTTPException(status_code=404, detail="Resultados no encontrados")
    
    results = job_results[job_id]
    return results

# Para pruebas locales
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)