"""
app.py
======
Dashboard do SIAV-Itaqui - painel Streamlit para monitorar, em tempo real,
os sensores de um Arduino Uno conectado por USB (leitura Serial), com
deteccao automatica de vazamentos, alertas por e-mail e rastreabilidade
do operador de turno.

Este arquivo foi escrito visando compatibilidade com versoes mais antigas
do Streamlit: evita st.divider(), use_container_width e hide_index (todos
relativamente recentes), e trata st.secrets de forma segura para o app
abrir mesmo sem um arquivo secrets.toml configurado.

Como rodar (localmente, com o Arduino conectado por USB):
    streamlit run app.py
"""

from __future__ import annotations

import os
import sys
import time
from collections import deque
from datetime import datetime

import pandas as pd
import serial
import streamlit as st

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from live_pipeline import SIAVPipeline, simulate_live_feed  # noqa: E402
from alert_center import AlertCenter, CRITICALITY_CHANNELS, enviar_email_alerta, _bloco_operador  # noqa: E402
import data_export  # noqa: E402

try:
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
    GSPREAD_DISPONIVEL = True
except Exception:
    GSPREAD_DISPONIVEL = False

st.set_page_config(page_title="SIAV-Itaqui", page_icon="🛟", layout="wide")

# ---------------------------------------------------------------------------
# Bypass de segredos: o app precisa abrir mesmo sem secrets.toml configurado.
# ---------------------------------------------------------------------------
try:
    _secrets = st.secrets if hasattr(st, "secrets") else {}
except Exception:
    _secrets = {}

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
SHEETS_HEADER = ["data_hora", "operador", "matricula", "turno", "status", "pressao", "vazao"]
SHEETS_FLUSH_INTERVAL_S = 2.0

ARDUINO_BASELINE_VIBRATION_MMS = 0.8
PORTA_SERIAL_PADRAO = "COM4"


class ArduinoConnectionError(Exception):
    """Erro ao abrir ou usar a porta serial do Arduino."""
    pass


# ---------------------------------------------------------------------------
# Leitura Serial do Arduino
# ---------------------------------------------------------------------------
def abrir_conexao_serial(porta, baudrate):
    try:
        conexao = serial.Serial(porta, baudrate=baudrate, timeout=2)
        conexao.reset_input_buffer()
        return conexao
    except serial.SerialException as exc:
        mensagem = (
            "Nao foi possivel abrir a porta " + str(porta) +
            " (baudrate " + str(baudrate) + "). Verifique se: "
            "(1) o Arduino esta conectado por USB; "
            "(2) a porta informada e a correta (confira no Gerenciador de "
            "Dispositivos do Windows); "
            "(3) nenhum outro programa esta usando a porta (ex.: Monitor "
            "Serial da Arduino IDE aberto). Detalhe tecnico: " + str(exc)
        )
        raise ArduinoConnectionError(mensagem) from exc


def ler_arduino_feed(conexao, duration_s, berco):
    inicio = time.time()
    while time.time() - inicio < duration_s:
        try:
            linha_bruta = conexao.readline().decode("utf-8", errors="ignore").strip()
        except serial.SerialException as exc:
            st.warning("Conexao com o Arduino perdida durante a leitura: " + str(exc))
            return

        if not linha_bruta:
            continue

        partes = linha_bruta.split(",")
        if len(partes) != 2:
            continue

        try:
            pressao = float(partes[0].strip())
            vazao = float(partes[1].strip())
        except ValueError:
            continue

        yield {
            "timestamp": datetime.now(),
            "berco": berco,
            "pressure_bar": pressao,
            "flow_m3h": vazao,
            "vibration_mms": ARDUINO_BASELINE_VIBRATION_MMS,
        }


# ---------------------------------------------------------------------------
# Google Sheets / Looker Studio (opcional)
# ---------------------------------------------------------------------------
def sheets_configurado():
    if not GSPREAD_DISPONIVEL:
        return False
    try:
        return "gcp_service_account" in _secrets
    except Exception:
        return False


def _obter_sheet():
    creds_dict = dict(_secrets["gcp_service_account"])
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    return client.open(GOOGLE_SHEETS_NAME).sheet1


def _garantir_cabecalho(sheet):
    valores_existentes = sheet.get_all_values()
    if not valores_existentes:
        sheet.append_row(SHEETS_HEADER)


def enviar_lote_para_sheets(linhas):
    if not linhas:
        return True
    try:
        sheet = _obter_sheet()
        _garantir_cabecalho(sheet)
        sheet.append_rows(linhas, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        st.error("Erro ao sincronizar com Google Sheets: " + str(e))
        return False


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
    for key in defaults:
        if key not in st.session_state:
            st.session_state[key] = defaults[key]

    if st.session_state.db_conn is None:
        conn = data_export.get_connection()
        data_export.init_db(conn)
        st.session_state.db_conn = conn


init_state()

# --- Barra lateral: fonte de dados ---
st.sidebar.title("🛟 SIAV-Itaqui")
st.sidebar.caption("Sistema Inteligente de Alerta a Vazamentos — Berços 104/108")

berco = st.sidebar.selectbox("Berço monitorado", ["104", "108"])

st.sidebar.markdown("---")
st.sidebar.subheader("🔌 Fonte de dados")
fonte_dados = st.sidebar.radio(
    "De onde vêm as leituras?",
    ["Simulação (cenários)", "Arduino (Leitura Serial USB)"],
)

if fonte_dados == "Simulação (cenários)":
    scenario = st.sidebar.selectbox(
        "Cenário a simular",
        list(SCENARIO_LABELS.keys()),
        format_func=lambda s: SCENARIO_LABELS[s],
    )
    speed = st.sidebar.slider(
        "Velocidade (segundos entre leituras)", 0.05, 1.0, 0.2,
        help="Menor = simulação mais rápida na tela"
    )
    porta_serial = None
    baudrate_serial = None
else:
    st.sidebar.warning(
        "Este modo so funciona rodando o Streamlit localmente, no "
        "computador fisicamente conectado ao Arduino por USB."
    )
    porta_serial = st.sidebar.text_input("Porta serial", value=PORTA_SERIAL_PADRAO)
    baudrate_serial = st.sidebar.number_input("Baudrate", value=115200, step=9600)
    scenario = None
    speed = None

duration_s = st.sidebar.slider("Duração da leitura (segundos)", 30, 180, 90)

if fonte_dados == "Arduino (Leitura Serial USB)":
    start_label = "▶️ Iniciar leitura do Arduino"
else:
    start_label = "▶️ Iniciar simulação"

start_button = st.sidebar.button(start_label, type="primary")
reset_button = st.sidebar.button("🔄 Resetar")

if reset_button:
    st.session_state.history.clear()
    st.session_state.alert_center = AlertCenter()
    st.session_state.confirmed_criticality = "Nenhum"
    st.session_state.sheets_sync_error_shown = False
    st.session_state.sheets_buffer = []
    st.session_state.sheets_last_flush_time = 0.0
    st.rerun()

# --- Barra lateral: identificação do operador de turno ---
st.sidebar.markdown("---")
st.sidebar.subheader("🧑‍💼 Identificação do Operador")

operador_nome = st.sidebar.text_input("Nome do Operador", placeholder="Ex.: João da Silva")
operador_matricula = st.sidebar.text_input("Matrícula/ID", placeholder="Ex.: OP-00123")
operador_turno = st.sidebar.selectbox(
    "Turno de Trabalho",
    ["Manhã (06h-14h)", "Tarde (14h-22h)", "Noite (22h-06h)", "Outro"],
)
operador_terminal = st.sidebar.text_input(
    "Terminal/Planta", placeholder="Ex.: Terminal Granel Químico — Itaqui"
)

operador_info = {
    "nome": operador_nome.strip(),
    "matricula": operador_matricula.strip(),
    "turno": operador_turno,
    "terminal": operador_terminal.strip(),
}
operador_completo = bool(operador_info["nome"] and operador_info["matricula"])

if not operador_completo:
    st.sidebar.warning("Preencha Nome e Matrícula do operador antes de iniciar.")

# --- Barra lateral: configuração de e-mail (brigada/gestão) ---
st.sidebar.markdown("---")
st.sidebar.subheader("📧 Alertas por e-mail")

email_enabled = st.sidebar.checkbox("Ativar envio de e-mail em alertas", value=False)

_email_padrao = ""
try:
    _email_padrao = _secrets.get("SIAV_EMAIL_DESTINATARIO", "")
except Exception:
    _email_padrao = ""

email_destinatario = st.sidebar.text_input(
    "E-mail da brigada/gestão",
    value=_email_padrao,
    placeholder="brigada@empresa.com",
)

smtp_expander = st.sidebar.expander("Configuração do servidor SMTP")
with smtp_expander:
    st.caption(
        "Dica: guarde essas credenciais em .streamlit/secrets.toml em vez "
        "de digitar toda vez. Ex.: SIAV_SMTP_HOST, SIAV_SMTP_USER, "
        "SIAV_SMTP_PASSWORD (use uma 'senha de app', nao a senha normal)."
    )
    try:
        _smtp_host_padrao = _secrets.get("SIAV_SMTP_HOST", "smtp.gmail.com")
        _smtp_port_padrao = int(_secrets.get("SIAV_SMTP_PORT", 587))
        _smtp_user_padrao = _secrets.get("SIAV_SMTP_USER", "")
        _smtp_password_padrao = _secrets.get("SIAV_SMTP_PASSWORD", "")
    except Exception:
        _smtp_host_padrao = "smtp.gmail.com"
        _smtp_port_padrao = 587
        _smtp_user_padrao = ""
        _smtp_password_padrao = ""

    smtp_host = st.text_input("Servidor SMTP", value=_smtp_host_padrao)
    smtp_port = st.number_input("Porta SMTP", value=_smtp_port_padrao, step=1)
    smtp_user = st.text_input("Usuário SMTP (remetente)", value=_smtp_user_padrao)
    smtp_password = st.text_input("Senha SMTP", value=_smtp_password_padrao, type="password")

test_email_button = st.sidebar.button("✉️ Enviar e-mail de teste")

if test_email_button:
    if not email_destinatario or not smtp_host or not smtp_user or not smtp_password:
        st.sidebar.error("Preencha o e-mail de destino e as credenciais SMTP antes de testar.")
    else:
        bloco_teste = _bloco_operador(operador_info)
        mensagem_teste = (
            "Este e um e-mail de teste do SIAV-Itaqui.\n\n"
            "Se voce recebeu esta mensagem, a configuracao de SMTP esta "
            "correta e os alertas de Microvazamento/Ruptura serao "
            "enviados normalmente para este endereco."
        )
        if bloco_teste:
            mensagem_teste = mensagem_teste + "\n\n" + bloco_teste

        ok, erro = enviar_email_alerta(
            destinatario=email_destinatario,
            assunto="[SIAV-Itaqui] E-mail de teste",
            mensagem=mensagem_teste,
            smtp_host=smtp_host,
            smtp_port=int(smtp_port),
            smtp_user=smtp_user,
            smtp_password=smtp_password,
        )
        if ok:
            st.sidebar.success("E-mail de teste enviado com sucesso!")
        else:
            st.sidebar.error("Falha ao enviar e-mail de teste: " + str(erro))

# --- Barra lateral: sincronização com Google Sheets/Looker (opcional) ---
st.sidebar.markdown("---")
st.sidebar.subheader("📊 Sincronização com Looker Studio")

sheets_secret_configurado = sheets_configurado()

if not GSPREAD_DISPONIVEL:
    st.sidebar.caption("Biblioteca gspread nao instalada; sincronizacao com Sheets desativada.")
    sheets_sync_enabled = False
else:
    sheets_sync_enabled = st.sidebar.checkbox(
        "Sincronizar automaticamente com Google Sheets",
        value=False,
    )
    if sheets_sync_enabled and not sheets_secret_configurado:
        st.sidebar.warning(
            "Secret 'gcp_service_account' nao encontrado. Configure em "
            "Settings > Secrets (Streamlit Cloud) ou em .streamlit/secrets.toml (local)."
        )

st.sidebar.markdown("---")
st.sidebar.caption(
    "Nota: durante a leitura/simulação, a tela roda de forma contínua até "
    "o fim do período escolhido."
)

# --- Corpo principal ---
st.title("Central de Monitoramento — SIAV-Itaqui")
st.caption("Desafio 2 — Detecção automática de vazamentos no Complexo Portuário do Itaqui")

status_placeholder = st.empty()
metrics_placeholder = st.empty()
chart_placeholder = st.empty()
st.subheader("Histórico de alertas")
alert_log_placeholder = st.empty()


def render_status(criticality):
    color = CRITICALITY_COLORS[criticality]
    channels = CRITICALITY_CHANNELS[criticality]
    if channels:
        channel_txt = ", ".join(channels)
    else:
        channel_txt = "Nenhum canal acionado"
    html = (
        '<div style="background-color:' + color + '22;border-left:8px solid ' + color + ';'
        'padding:16px 20px;border-radius:8px;margin-bottom:16px;">'
        '<span style="font-size:1.3em;font-weight:700;color:' + color + ';">'
        'Criticidade atual: ' + criticality + '</span><br/>'
        '<span style="font-size:0.95em;color:#444;">Canais de alerta: ' + channel_txt + '</span>'
        '</div>'
    )
    status_placeholder.markdown(html, unsafe_allow_html=True)


def render_metrics(result):
    with metrics_placeholder.container():
        cols = st.columns(4)
        cols[0].metric("Pressão (bar)", "{:.2f}".format(result["pressure_bar"]))
        cols[1].metric("Vazão (m³/h)", "{:.1f}".format(result["flow_m3h"]))
        cols[2].metric("Vibração (mm/s)", "{:.2f}".format(result["vibration_mms"]))
        cols[3].metric("Classificação do modelo", result["predicted_label"])


def render_chart(history):
    if not history:
        chart_placeholder.info("Clique em Iniciar na barra lateral para começar.")
        return
    df = pd.DataFrame(history)
    chart_placeholder.line_chart(df.set_index("timestamp")[["pressure_bar"]])


def render_alert_log():
    center = st.session_state.alert_center
    if center.log:
        rows = []
        for a in center.log:
            rows.append({
                "Horário": a.timestamp.strftime("%H:%M:%S"),
                "Berço": a.berco,
                "Estado detectado": a.predicted_label,
                "Criticidade": a.criticality,
                "Canais acionados": ", ".join(a.channels) if a.channels else "-",
                "E-mail": a.email_status if a.email_status else ("-" if "Email" not in a.channels else "Pendente"),
                "Operador": a.operador_nome if a.operador_nome else "-",
                "Matrícula": a.operador_matricula if a.operador_matricula else "-",
                "Turno": a.turno if a.turno else "-",
            })
        df_rows = pd.DataFrame(rows)
        df_rows = df_rows[::-1]
        alert_log_placeholder.dataframe(df_rows)
    else:
        alert_log_placeholder.info("Nenhum alerta emitido ainda nesta simulação.")


def render_last_alert_messages():
    center = st.session_state.alert_center
    if not center.log:
        message_placeholder.empty()
        return
    last = center.log[-1]
    with message_placeholder.container():
        st.markdown("**Prévia das mensagens do último alerta** (criticidade: " + last.criticality + ")")
        for canal in last.messages:
            msg = last.messages[canal]
            st.text("[" + canal + "]")
            st.code(msg, language=None)
        if last.email_status:
            st.caption("Status do e-mail: " + last.email_status)


def despachar_email_se_preciso(alert):
    if "Email" not in alert.channels:
        return
    if not email_enabled:
        alert.email_status = "Desativado (ative na barra lateral)"
        return
    if not email_destinatario or not smtp_host or not smtp_user or not smtp_password:
        alert.email_status = "Não enviado (configuração incompleta)"
        return

    corpo_email = alert.messages.get("Email", "")
    if corpo_email.startswith("Assunto: "):
        partes = corpo_email.split("\n\n", 1)
        assunto = partes[0][len("Assunto: "):]
        if len(partes) > 1:
            corpo = partes[1]
        else:
            corpo = ""
    else:
        assunto = "[SIAV-Itaqui] Alerta " + alert.criticality + " — Berço " + alert.berco
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
    if ok:
        alert.email_status = "Enviado com sucesso"
    else:
        alert.email_status = "Falha: " + str(erro)


def bufferizar_leitura_para_sheets(result):
    if not sheets_sync_enabled or not sheets_secret_configurado:
        return
    if st.session_state.sheets_sync_error_shown:
        return

    linha = [
        str(result["timestamp"]),
        str(operador_info["nome"]),
        str(operador_info["matricula"]),
        str(operador_info["turno"]),
        str(result["predicted_label"]),
        float(result["pressure_bar"]),
        float(result["flow_m3h"]),
    ]
    st.session_state.sheets_buffer.append(linha)

    agora = time.time()
    if agora - st.session_state.sheets_last_flush_time >= SHEETS_FLUSH_INTERVAL_S:
        flush_buffer_sheets()


def flush_buffer_sheets():
    if not st.session_state.sheets_buffer:
        st.session_state.sheets_last_flush_time = time.time()
        return

    ok = enviar_lote_para_sheets(st.session_state.sheets_buffer)
    if ok:
        st.session_state.sheets_buffer = []
        st.session_state.sheets_last_flush_time = time.time()
    else:
        st.session_state.sheets_sync_error_shown = True


def processar_leitura(reading):
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


# --- Estado inicial (antes de qualquer leitura rodar) ---
render_status(st.session_state.confirmed_criticality)
if st.session_state.history:
    render_metrics(st.session_state.history[-1])
    render_chart(list(st.session_state.history))
else:
    metrics_placeholder.empty()
    chart_placeholder.info("Clique em Iniciar na barra lateral para começar.")
render_alert_log()
st.subheader("Prévia das mensagens (SMS / WhatsApp simulados / E-mail)")
message_placeholder = st.empty()
render_last_alert_messages()

# --- Loop principal (simulação OU leitura real do Arduino) ---
if start_button and not operador_completo:
    st.error(
        "Não é possível iniciar sem a identificação do operador de turno "
        "(Nome e Matrícula) — rastreabilidade obrigatória para o relatório de SMS."
    )
elif start_button:
    st.session_state.pipeline = SIAVPipeline()
    st.session_state.history.clear()
    st.session_state.alert_center = AlertCenter()
    st.session_state.confirmed_criticality = "Nenhum"
    st.session_state.sheets_sync_error_shown = False
    st.session_state.sheets_buffer = []
    st.session_state.sheets_last_flush_time = time.time()

    st.caption(
        "Turno em operação: " + operador_info["nome"] +
        " (matrícula " + operador_info["matricula"] + ") — " +
        operador_info["turno"] + " — " +
        (operador_info["terminal"] if operador_info["terminal"] else "terminal não informado")
    )

    if fonte_dados == "Arduino (Leitura Serial USB)":
        conexao_serial = None
        try:
            conexao_serial = abrir_conexao_serial(porta_serial, int(baudrate_serial))
        except ArduinoConnectionError as exc:
            st.error(str(exc))

        if conexao_serial is not None:
            st.info("Lendo do Arduino na porta " + str(porta_serial) + " (" + str(int(baudrate_serial)) + " baud)...")
            try:
                for reading in ler_arduino_feed(conexao_serial, duration_s, berco):
                    processar_leitura(reading)
            finally:
                conexao_serial.close()
    else:
        for reading in simulate_live_feed(scenario_label=scenario, berco=berco, duration_s=duration_s):
            processar_leitura(reading)
            time.sleep(speed)

    if sheets_sync_enabled and sheets_secret_configurado and not st.session_state.sheets_sync_error_shown:
        flush_buffer_sheets()

    st.success("Leitura/simulação concluída.")
    st.caption(
        "Leituras salvas em " + data_export.READINGS_CSV + " / alertas em " +
        data_export.ALERTS_CSV + " (e no SQLite " + data_export.DB_PATH + "), "
        "prontos para o Looker Studio."
    )
    if sheets_sync_enabled and sheets_secret_configurado:
        if st.session_state.sheets_sync_error_shown:
            st.caption("A sincronização com o Google Sheets encontrou um erro durante a leitura (veja acima).")
        else:
            st.caption("Leituras também sincronizadas em lote com a planilha " + GOOGLE_SHEETS_NAME + ".")

# --- Download dos CSVs para o Looker Studio (caminho manual) ---
st.markdown("---")
st.subheader("📥 Exportar dados para o Looker Studio (manual)")
st.caption(
    "Baixe os CSVs atualizados e suba-os no Google Drive (ou em uma Planilha "
    "Google) para conectar ao Looker Studio."
)

col_dl1, col_dl2 = st.columns(2)

if os.path.exists(data_export.READINGS_CSV):
    arquivo_leituras = open(data_export.READINGS_CSV, "rb")
    dados_leituras = arquivo_leituras.read()
    arquivo_leituras.close()
    col_dl1.download_button(
        "⬇️ Baixar live_readings.csv",
        data=dados_leituras,
        file_name="live_readings.csv",
        mime="text/csv",
    )
else:
    col_dl1.info("Ainda não há leituras salvas. Rode uma simulação/leitura primeiro.")

if os.path.exists(data_export.ALERTS_CSV):
    arquivo_alertas = open(data_export.ALERTS_CSV, "rb")
    dados_alertas = arquivo_alertas.read()
    arquivo_alertas.close()
    col_dl2.download_button(
        "⬇️ Baixar live_alerts.csv",
        data=dados_alertas,
        file_name="live_alerts.csv",
        mime="text/csv",
    )
else:
    col_dl2.info("Ainda não há alertas salvos. Rode uma simulação/leitura com anomalia primeiro.")
