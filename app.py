"""
app.py
======
Dashboard do SIAV-Itaqui — Central de Monitoramento em Tempo Real.

Suporta duas fontes de dados:
  1. Simulação sintética (para demonstração / Streamlit Cloud)
  2. Arduino Uno via USB (para testes de bancada física local)
"""

from __future__ import annotations

import os
import sys
import time
from collections import deque
from datetime import datetime

import gspread
import pandas as pd
import serial
import streamlit as st
from oauth2client.service_account import ServiceAccountCredentials

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from live_pipeline import SIAVPipeline, simulate_live_feed  # noqa: E402
from alert_center import AlertCenter, CRITICALITY_CHANNELS, enviar_email_alerta, _bloco_operador  # noqa: E402
import data_export  # noqa: E402

st.set_page_config(page_title="SIAV-Itaqui", page_icon="🛟", layout="wide")

CRITICALITY_COLORS = {
    "Nenhum": "#2ecc71",
    "Baixa": "#f1c40f",
    "Média": "#e67e22",
    "Crítica": "#e74c3c",
}

SCENARIO_LABELS = {
    "normal": "Operação normal",
    "microvazamento": "Microvazamento",
    "ruptura_parcial": "Ruptura parcial",
    "ruptura_total": "Ruptura total",
}

GOOGLE_SHEETS_NAME = "SIAV_Telemetria_Looker"
SHEETS_HEADER = ["data_hora", "pressao", "vazao", "status", "operador", "matricula", "turno"]
SHEETS_FLUSH_INTERVAL_S = 2.0  # intervalo mínimo entre envios em lote


def _obter_sheet():
    creds_dict = dict(st.secrets["gcp_service_account"])
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    return client.open(GOOGLE_SHEETS_NAME).sheet1


def _garantir_cabecalho(sheet) -> None:
    valores_existentes = sheet.get_all_values()
    if not valores_existentes:
        sheet.append_row(SHEETS_HEADER)


def enviar_lote_para_sheets(linhas: list[list]) -> bool:
    if not linhas:
        return True
    try:
        sheet = _obter_sheet()
        _garantir_cabecalho(sheet)
        sheet.append_rows(linhas, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        st.error(f"Erro ao sincronizar com Google Sheets: {e}")
        return False



def conectar_arduino(porta: str, baudrate: int = 115200):
    try:
        ser = serial.Serial(porta, baudrate, timeout=1)
        time.sleep(2)  # Aguarda reset do Arduino ao abrir serial
        return ser
    except Exception as e:
        st.sidebar.error(f"Erro ao conectar na porta {porta}: {e}")
        return None


def init_state():
    defaults = {
        "pipeline": None,
        "history": deque(maxlen=150),
        "alert_center": AlertCenter(),
        "confirmed_criticality": "Nenhum",
        "db_conn": None,
        "sheets_sync_error_shown": False,
        "sheets_buffer": [],
        "sheets_last_flush_time": 0.0,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

    if st.session_state.db_conn is None:
        conn = data_export.get_connection()
        data_export.init_db(conn)
        st.session_state.db_conn = conn


init_state()

# --- Barra lateral: seleção de fonte e controles ---
st.sidebar.title("🛟 SIAV-Itaqui")
st.sidebar.caption("Sistema Inteligente de Alerta a Vazamentos — Berços 104/108")

berco = st.sidebar.selectbox("Berço monitorado", ["104", "108"])

# SELETOR DE FONTE DE DADOS (Simulação vs Arduino Local)
fonte_dados = st.sidebar.radio(
    "Fonte de Dados",
    ["Simulação Sintética", "Arduino USB (Bancada Local)"],
    help="Para usar o Arduino real, execute o Streamlit na sua máquina física via cabo USB."
)

if fonte_dados == "Simulação Sintética":
    scenario = st.sidebar.selectbox(
        "Cenário a simular",
        list(SCENARIO_LABELS.keys()),
        format_func=lambda s: SCENARIO_LABELS[s],
    )
    duration_s = st.sidebar.slider("Duração da simulação (segundos)", 30, 180, 90)
    speed = st.sidebar.slider("Velocidade (segundos entre leituras)", 0.05, 1.0, 0.2)
else:
    porta_com = st.sidebar.text_input("Porta COM (ex: COM3 ou /dev/ttyACM0)", value="COM3")
    ser_arduino = conectar_arduino(porta_com)
    if ser_arduino and ser_arduino.is_open:
        st.sidebar.success(f"Conectado ao Arduino na {porta_com}")

start_button = st.sidebar.button("▶️ Iniciar Captura/Simulação", use_container_width=True, type="primary")
reset_button = st.sidebar.button("🔄 Resetar", use_container_width=True)

if reset_button:
    st.session_state.history.clear()
    st.session_state.alert_center = AlertCenter()
    st.session_state.confirmed_criticality = "Nenhum"
    st.session_state.sheets_sync_error_shown = False
    st.session_state.sheets_buffer = []
    st.session_state.sheets_last_flush_time = 0.0
    st.rerun()

# --- Identificação do operador ---
st.sidebar.divider()
st.sidebar.subheader("🧑‍💼 Identificação do Operador")

operador_nome = st.sidebar.text_input("Nome do Operador", placeholder="Ex.: João da Silva")
operador_matricula = st.sidebar.text_input("Matrícula/ID", placeholder="Ex.: OP-00123")
operador_turno = st.sidebar.selectbox(
    "Turno de Trabalho",
    ["Manhã (06h-14h)", "Tarde (14h-22h)", "Noite (22h-06h)", "Outro"],
)
operador_terminal = st.sidebar.text_input("Terminal/Planta", placeholder="Ex.: Terminal Granel Químico — Itaqui")

operador_info = {
    "nome": operador_nome.strip(),
    "matricula": operador_matricula.strip(),
    "turno": operador_turno,
    "terminal": operador_terminal.strip(),
}
operador_completo = bool(operador_info["nome"] and operador_info["matricula"])

if not operador_completo:
    st.sidebar.warning("Preencha Nome e Matrícula do operador antes de iniciar.")

# --- Configuração de e-mail ---
st.sidebar.divider()
st.sidebar.subheader("📧 Alertas por e-mail")

email_enabled = st.sidebar.checkbox("Ativar envio de e-mail em alertas", value=False)
email_destinatario = st.sidebar.text_input(
    "E-mail da brigada/gestão",
    value=st.secrets.get("SIAV_EMAIL_DESTINATARIO", "") if hasattr(st, "secrets") else "",
    placeholder="brigada@empresa.com",
)

with st.sidebar.expander("Configuração do servidor SMTP"):
    _secrets = st.secrets if hasattr(st, "secrets") else {}
    smtp_host = st.text_input("Servidor SMTP", value=_secrets.get("SIAV_SMTP_HOST", "smtp.gmail.com"))
    smtp_port = st.number_input("Porta SMTP", value=int(_secrets.get("SIAV_SMTP_PORT", 587)), step=1)
    smtp_user = st.text_input("Usuário SMTP (remetente)", value=_secrets.get("SIAV_SMTP_USER", ""))
    smtp_password = st.text_input("Senha SMTP", value=_secrets.get("SIAV_SMTP_PASSWORD", ""), type="password")

test_email_button = st.sidebar.button("✉️ Enviar e-mail de teste", use_container_width=True)

if test_email_button:
    if not email_destinatario or not smtp_host or not smtp_user or not smtp_password:
        st.sidebar.error("Preencha o e-mail de destino e as credenciais SMTP antes de testar.")
    else:
        _bloco_teste = _bloco_operador(operador_info)
        _mensagem_teste = "Este é um e-mail de teste do SIAV-Itaqui."
        if _bloco_teste:
            _mensagem_teste = f"{_mensagem_teste}\n\n{_bloco_teste}"

        ok, erro = enviar_email_alerta(
            destinatario=email_destinatario,
            assunto="[SIAV-Itaqui] E-mail de teste",
            mensagem=_mensagem_teste,
            smtp_host=smtp_host,
            smtp_port=int(smtp_port),
            smtp_user=smtp_user,
            smtp_password=smtp_password,
        )
        if ok:
            st.sidebar.success("E-mail de teste enviado com sucesso!")
        else:
            st.sidebar.error(f"Falha ao enviar e-mail de teste: {erro}")

# --- Google Sheets / Looker ---
st.sidebar.divider()
st.sidebar.subheader("📊 Sincronização com Looker Studio")

sheets_secret_configurado = hasattr(st, "secrets") and "gcp_service_account" in st.secrets
sheets_sync_enabled = st.sidebar.checkbox("Sincronizar automaticamente com Google Sheets", value=True)

if sheets_sync_enabled and not sheets_secret_configurado:
    st.sidebar.warning("Secret `gcp_service_account` não encontrado.")

# --- Corpo Principal ---
st.title("Central de Monitoramento — SIAV-Itaqui")
st.caption("Desafio 2 — Detecção automática de vazamentos no Complexo Portuário do Itaqui")

status_placeholder = st.empty()
metrics_placeholder = st.empty()
chart_placeholder = st.empty()
st.subheader("Histórico de alertas")
alert_log_placeholder = st.empty()


def render_status(criticality: str):
    color = CRITICALITY_COLORS[criticality]
    channels = CRITICALITY_CHANNELS[criticality]
    channel_txt = ", ".join(channels) if channels else "Nenhum canal acionado"
    status_placeholder.markdown(
        f"""
        <div style="background-color:{color}22;border-left:8px solid {color};
                    padding:16px 20px;border-radius:8px;margin-bottom:16px;">
            <span style="font-size:1.3em;font-weight:700;color:{color};">
                Criticidade atual: {criticality}
            </span><br/>
            <span style="font-size:0.95em;color:#444;">Canais de alerta: {channel_txt}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_metrics(result: dict):
    with metrics_placeholder.container():
        cols = st.columns(4)
        cols[0].metric("Pressão (PSI / bar)", f"{result['pressure_bar']:.2f}")
        cols[1].metric("Vazão (L/min ou m³/h)", f"{result['flow_m3h']:.1f}")
        cols[2].metric("Vibração (mm/s)", f"{result.get('vibration_mms', 0.0):.2f}")
        cols[3].metric("Classificação do modelo", result["predicted_label"])


def render_chart(history: list[dict]):
    if not history:
        chart_placeholder.info("Aguardando leituras para exibir o gráfico.")
        return
    df = pd.DataFrame(history)
    chart_placeholder.line_chart(df.set_index("timestamp")[["pressure_bar"]])


def render_alert_log():
    center = st.session_state.alert_center
    if center.log:
        rows = [
            {
                "Horário": a.timestamp.strftime("%H:%M:%S"),
                "Berço": a.berco,
                "Estado detectado": a.predicted_label,
                "Criticidade": a.criticality,
                "Canais acionados": ", ".join(a.channels) or "—",
                "E-mail": a.email_status or ("—" if "Email" not in a.channels else "Pendente"),
                "Operador": a.operador_nome or "—",
                "Matrícula": a.operador_matricula or "—",
                "Turno": a.turno or "—",
            }
            for a in center.log
        ]
        alert_log_placeholder.dataframe(pd.DataFrame(rows)[::-1], use_container_width=True, hide_index=True)
    else:
        alert_log_placeholder.info("Nenhum alerta emitido ainda.")


def render_last_alert_messages():
    center = st.session_state.alert_center
    if not center.log:
        message_placeholder.empty()
        return
    last = center.log[-1]
    with message_placeholder.container():
        st.markdown(f"**Prévia das mensagens do último alerta** (criticidade: {last.criticality})")
        for canal, msg in last.messages.items():
            st.text(f"[{canal}]")
            st.code(msg, language=None)


def despachar_email_se_preciso(alert):
    if "Email" not in alert.channels or not email_enabled:
        return
    if not email_destinatario or not smtp_host or not smtp_user or not smtp_password:
        alert.email_status = "Não enviado (configuração incompleta)"
        return

    corpo_email = alert.messages.get("Email", "")
    if corpo_email.startswith("Assunto: "):
        assunto_linha, _, corpo = corpo_email.partition("\n\n")
        assunto = assunto_linha[len("Assunto: "):]
    else:
        assunto = f"[SIAV-Itaqui] Alerta {alert.criticality} — Berço {alert.berco}"
        corpo = corpo_email

    ok, erro = enviar_email_alerta(
        destinatario=email_destinatario,
        assunto=assunto,
        mensagem=corpo,
        smtp_host=smtp_host,
        smtp_port=int(smtp_port),
        smtp_user=smtp_user,
        smtp_password=smtp_password,
    )
    alert.email_status = "Enviado com sucesso" if ok else f"Falha: {erro}"


def bufferizar_leitura_para_sheets(result: dict) -> None:
    if not sheets_sync_enabled or not sheets_secret_configurado or st.session_state.sheets_sync_error_shown:
        return

    linha = [
        str(result["timestamp"]),
        float(result["pressure_bar"]),
        float(result["flow_m3h"]),
        str(result["predicted_label"]),
        str(operador_info["nome"]),
        str(operador_info["matricula"]),
        str(operador_info["turno"]),
    ]
    st.session_state.sheets_buffer.append(linha)

    agora = time.time()
    if agora - st.session_state.sheets_last_flush_time >= SHEETS_FLUSH_INTERVAL_S:
        flush_buffer_sheets()


def flush_buffer_sheets() -> None:
    if not st.session_state.sheets_buffer:
        st.session_state.sheets_last_flush_time = time.time()
        return

    ok = enviar_lote_para_sheets(st.session_state.sheets_buffer)
    if ok:
        st.session_state.sheets_buffer = []
        st.session_state.sheets_last_flush_time = time.time()
    else:
        st.session_state.sheets_sync_error_shown = True


# --- Estado inicial ---
render_status(st.session_state.confirmed_criticality)
if st.session_state.history:
    render_metrics(st.session_state.history[-1])
    render_chart(list(st.session_state.history))
else:
    metrics_placeholder.empty()
    chart_placeholder.info("Clique em ▶️ Iniciar Captura/Simulação para começar.")
render_alert_log()
st.subheader("Prévia das mensagens (SMS / WhatsApp simulados / E-mail)")
message_placeholder = st.empty()
render_last_alert_messages()

# --- Execução Principal ---
if start_button and not operador_completo:
    st.error("Preencha Nome e Matrícula do operador para continuar.")

elif start_button:
    st.session_state.pipeline = SIAVPipeline()
    st.session_state.history.clear()
    st.session_state.alert_center = AlertCenter()
    st.session_state.confirmed_criticality = "Nenhum"
    st.session_state.sheets_sync_error_shown = False
    st.session_state.sheets_buffer = []
    st.session_state.sheets_last_flush_time = time.time()

    # MODO 1: SIMULAÇÃO SINTÉTICA
    if fonte_dados == "Simulação Sintética":
        for reading in simulate_live_feed(scenario_label=scenario, berco=berco, duration_s=duration_s):
            result = st.session_state.pipeline.process_reading(reading)
            st.session_state.history.append(result)
            data_export.save_reading(st.session_state.db_conn, result)
            bufferizar_leitura_para_sheets(result)

            alert = st.session_state.alert_center.process(result, operador=operador_info)
            if alert:
                st.session_state.confirmed_criticality = alert.criticality
                despachar_email_se_preciso(alert)
                data_export.save_alert(st.session_state.db_conn, alert)

            render_status(st.session_state.confirmed_criticality)
            render_metrics(result)
            render_chart(list(st.session_state.history))
            render_alert_log()
            render_last_alert_messages()

            time.sleep(speed)

    # MODO 2: ARDUINO USB REAL (BANCADA)
    else:
        st.info("Coletando telemetria real do Arduino USB. Pressione Stop no topo do aplicativo para interromper.")
        ser = conectar_arduino(porta_com)

        if ser and ser.is_open:
            while True:
                if ser.in_waiting > 0:
                    try:
                        linha = ser.readline().decode("utf-8").strip()
                        dados = linha.split(",")

                        if len(dados) == 2:
                            pressao_val = float(dados[0])
                            vazao_val = float(dados[1])

                            reading = {
                                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "pressure_bar": pressao_val,
                                "flow_m3h": vazao_val,
                                "vibration_mms": 0.0,
                                "berco": berco,
                            }

                            result = st.session_state.pipeline.process_reading(reading)
                            st.session_state.history.append(result)
                            data_export.save_reading(st.session_state.db_conn, result)
                            bufferizar_leitura_para_sheets(result)

                            alert = st.session_state.alert_center.process(result, operador=operador_info)
                            if alert:
                                st.session_state.confirmed_criticality = alert.criticality
                                despachar_email_se_preciso(alert)
                                data_export.save_alert(st.session_state.db_conn, alert)

                            render_status(st.session_state.confirmed_criticality)
                            render_metrics(result)
                            render_chart(list(st.session_state.history))
                            render_alert_log()
                            render_last_alert_messages()

                    except Exception as e:
                        st.warning(f"Aguardando sinal estável do Arduino... ({e})")

                time.sleep(0.2)

    # Flush final ao encerrar
    if sheets_sync_enabled and sheets_secret_configurado and not st.session_state.sheets_sync_error_shown:
        flush_buffer_sheets()

    st.success("Coleta/Simulação concluída.")

# --- Download manual ---
st.divider()
st.subheader("📥 Exportar dados para o Looker Studio (manual)")
col_dl1, col_dl2 = st.columns(2)

if os.path.exists(data_export.READINGS_CSV):
    with open(data_export.READINGS_CSV, "rb") as f:
        col_dl1.download_button(
            "⬇️ Baixar live_readings.csv",
            data=f.read(),
            file_name="live_readings.csv",
            mime="text/csv",
            use_container_width=True,
        )

if os.path.exists(data_export.ALERTS_CSV):
    with open(data_export.ALERTS_CSV, "rb") as f:
        col_dl2.download_button(
            "⬇️ Baixar live_alerts.csv",
            data=f.read(),
            file_name="live_alerts.csv",
            mime="text/csv",
            use_container_width=True,
        )
