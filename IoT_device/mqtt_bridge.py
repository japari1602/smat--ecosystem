import json
import sys
import threading
import time
import paho.mqtt.client as mqtt
import requests

# ==============================================================================
# CONFIGURACIÓN DEL ENTORNO SMAT
# ==============================================================================
MQTT_BROKER = "broker.hivemq.com"
MQTT_PORT = 1883
MQTT_TOPIC = "fisi/smat/estaciones/+/lecturas"
API_URL = "http://localhost:8000/lecturas/"
JWT_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhZG1pbl9maXNpIiwiZXhwIjoxNzgxMTEwNzc4fQ.DALpKNFL_2wGf3UKAuRua5yKMsvdurwi3bMo9dExttM"

# Umbrales del Filtro Deadband
UMBRAL_CAMBIO_PORCENTAJE = 0.05  # ± 5%
TIEMPO_MAXIMO_REPORTE = 60.0    # 60 segundos para garantizar reporte de vida

# ==============================================================================
# MEMORIA CACHÉ LOCAL
# ==============================================================================
# last_seen: RASTREA INACTIVIDAD CRÍTICA (offline)
#   Estructura: {estacion_id: timestamp_ultimo_mensaje}
last_seen = {}

# db_cache: ALGORITMO DE FILTRO (Deadband + Tiempo de vida)
#   Estructura: {estacion_id: {"valor": float, "last_db_save": timestamp}}
db_cache = {}

# ==============================================================================
# MONITOREO DE INACTIVIDAD GENERAL (HILO SECUNDARIO)
# ==============================================================================
def check_deadlines():
    """Hilo secundario que revisa continuamente si alguna estación dejó de transmitir por completo."""
    print("🕵️ Hilo de monitoreo de alertas iniciado...")
    while True:
        current_time = time.time()
        for eid, last_time in list(last_seen.items()):
            if current_time - last_time > 30:  # Alerta si no se recibe nada de nada en 30s
                print(f"🚨 ALERTA: Estación {eid} está OFFLINE (Sin comunicación por {int(current_time - last_time)}s)")
        time.sleep(10)

# ==============================================================================
# CALLBACKS DE RED MQTT Y LÓGICA DEL FILTRO DE UMBRAL
# ==============================================================================
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("🟢 Conectado exitosamente al Broker MQTT")
        client.subscribe(MQTT_TOPIC)
        print(f"📡 Escuchando transmisiones en el tópico: {MQTT_TOPIC}")
    else:
        print(f"🔴 Error de conexión al Broker. Código de retorno: {rc}")
        sys.exit(1)

def on_message(client, userdata, msg):
    try:
        # 1. Decodificar el payload binario a JSON
        payload_raw = msg.payload.decode("utf-8")
        data_json = json.loads(payload_raw)
        
        # 2. Extraer el ID dinámico de la estación
        topic_parts = msg.topic.split('/')
        estacion_id = int(topic_parts[3])
        nuevo_valor = float(data_json["valor"])
        
        current_time = time.time()
        
        # Actualizar el registro general de vida de la estación (para el hilo offline)
        last_seen[estacion_id] = current_time
        
        print(f"📩 Telemetría recibida de Estación [{estacion_id}]: {nuevo_valor} cm")

        # ==============================================================================
        # APLICACIÓN DEL ALGORITMO DEADBAND FILTER
        # ==============================================================================
        enviar_a_db = False
        razon_envio = ""

        # Caso A: Si la estación es nueva en la caché, se guarda sí o sí
        if estacion_id not in db_cache:
            enviar_a_db = True
            razon_envio = "Primer reporte de la estación (Inicialización de caché)"
        else:
            datos_anteriores = db_cache[estacion_id]
            ultimo_valor_guardado = datos_anteriores["valor"]
            ultimo_tiempo_guardado = datos_anteriores["last_db_save"]

            # Calcular la variación porcentual absoluta
            # Evitamos división por cero si el último valor fue 0
            if ultimo_valor_guardado != 0:
                variacion = abs(nuevo_valor - ultimo_valor_guardado) / ultimo_valor_guardado
            else:
                variacion = abs(nuevo_valor - ultimo_valor_guardado)

            tiempo_transcurrido = current_time - ultimo_tiempo_guardado

            # Evaluación de condiciones:
            # Condición 1: Variación mayor al 5%
            if variacion >= UMBRAL_CAMBIO_PORCENTAJE:
                enviar_a_db = True
                razon_envio = f"Variación significativa detectada: {variacion*100:.2f}% (Umbral ±5%)"
            
            # Condición 2: Pasaron más de 60 segundos desde el último guardado
            elif tiempo_transcurrido >= TIEMPO_MAXIMO_REPORTE:
                enviar_a_db = True
                razon_envio = f"Límite de tiempo alcanzado ({int(tiempo_transcurrido)}s sin reportar). Forzando latido de vida."

        # ==============================================================================
        # PROCESAMIENTO DEL ENVÍO O BLOQUEO
        # ==============================================================================
        if enviar_a_db:
            print(f"⚙️  [Filtro: PASA] {razon_envio}")
            
            # Formatear la carga útil para FastAPI
            api_payload = {
                "valor": nuevo_valor,
                "estacion_id": estacion_id
            }
            
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {JWT_TOKEN}"
            }
            
            response = requests.post(API_URL, json=api_payload, headers=headers)
            
            if response.status_code in [200, 201]:
                print(f"💾 [DB Sincronizada] Guardado en SQLite: {nuevo_valor} cm.")
                # Actualizar caché local tras el éxito de persistencia
                db_cache[estacion_id] = {
                    "valor": nuevo_valor,
                    "last_db_save": current_time
                }
            else:
                print(f"⚠️ [Fallo de Ingesta] API rechazó el dato. Código: {response.status_code}")
        else:
            # Log de validación del bloqueo
            tiempo_sig = TIEMPO_MAXIMO_REPORTE - (current_time - db_cache[estacion_id]["last_db_save"])
            print(f"🛑 [Filtro: BLOQUEADO] Variación insignificante. Redundancia descartada. "
                  f"(Próximo reporte forzado en: {int(tiempo_sig)}s)")

    except KeyError as e:
        print(f"❌ Error de esquema: Falta la llave {e} en el payload MQTT.")
    except ValueError:
        print("❌ Error de casteo: El valor o el ID de la estación no son numéricos.")
    except Exception as e:
        print(f"❌ Error crítico en el Bridge: {e}")

# ==============================================================================
# EJECUCIÓN PRINCIPAL
# ==============================================================================
if __name__ == "__main__":
    bridge_client = mqtt.Client()
    bridge_client.on_connect = on_connect
    bridge_client.on_message = on_message

    try:
        print("🚀 Inicializando el Bridge de Acoplamiento SMAT con Filtro Deadband...")
        
        monitor_thread = threading.Thread(target=check_deadlines, daemon=True)
        monitor_thread.start()
        
        bridge_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        bridge_client.loop_forever()
        
    except KeyboardInterrupt:
        print("\n🛑 Bridge detenido manualmente por el administrador.")