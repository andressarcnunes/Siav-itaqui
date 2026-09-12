"""
app.py
======
Dashboard do SIAV-Itaqui — a interface que o Centro de Controle Operacional
(CCO) veria na prática, e que a banca vai ver rodando na demonstração.

Mostra, em tempo real (simulado):
  - Gráfico de pressão do berço monitorado
  - Métricas atuais de pressão, vazão e vibração
  - Criticidade atual (Nenhum / Baixa / Média / Crítica), com os canais
    de alerta que seriam acionados em cada nível (Dashboard / SMS /
    WhatsApp simulados + E-mail real via SMTP)
  - Histórico dos alertas emitidos durante a simulação, incluindo o
    status do envio de e-mail
  - Persistência automática de leituras e alertas em SQLite + CSV, para
    alimentar relatórios no Google Looker Studio (ver data_export.py)

Como rodar:
    streamlit run app.py
"""

from __future__ import annotations

import os
import sys
import time
from collections import deque

import pandas as pd
import streamlit as st

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


def init_state():
    defaults = {
        "pipeline": None,
        "history": deque(maxlen=150),
        "alert_center": AlertCenter(),
        "confirmed_criticality": "Nenhum",
        "db_conn": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

    if st.session_state.db_conn is None:
        conn = data_export.get_connection()
        data_export.init_db(conn)
        st.session_state.db_conn = conn


init_state()

# --- Barra lateral: controles da simulação ---
st.sidebar.title("🛟 SIAV-Itaqui")
st.sidebar.caption("Sistema Inteligente de Alerta a Vazamentos — Berços 104/108")

berco = st.sidebar.selectbox("Berço monitorado", ["104", "108"])
scenario = st.sidebar.selectbox(
    "Cenário a simular",
    list(SCENARIO_LABELS.keys()),
    format_func=lambda s: SCENARIO_LABELS[s],
)
duration_s = st.sidebar.slider("Duração da simulação (segundos)", 30, 180, 90)
speed = st.sidebar.slider(
    "Velocidade (segundos entre leituras)", 0.05, 1.0, 0.2,
    help="Menor = simulação mais rápida na tela"
)

start_button = st.sidebar.button("▶️ Iniciar simulação", use_container_width=True, type="primary")
reset_button = st.sidebar.button("🔄 Resetar", use_container_width=True)

if reset_button:
    st.session_state.history.clear()
    st.session_state.alert_center = AlertCenter()
    st.session_state.confirmed_criticality = "Nenhum"
    st.rerun()

# --- Barra lateral: identificação do operador de turno ---
# Rastreabilidade exigida pela Granel Química: todo alerta despachado
# durante a simulação carrega quem estava no turno no momento do disparo.
st.sidebar.divider()
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
    st.sidebar.warning("Preencha Nome e Matrícula do operador antes de iniciar a simulação.")

# --- Barra lateral: configuração de e-mail (brigada/gestão) ---
st.sidebar.divider()
st.sidebar.subheader("📧 Alertas por e-mail")

email_enabled = st.sidebar.checkbox("Ativar envio de e-mail em alertas", value=False)
email_destinatario = st.sidebar.text_input(
    "E-mail da brigada/gestão",
    value=st.secrets.get("SIAV_EMAIL_DESTINATARIO", "") if hasattr(st, "secrets") else "",
    placeholder="brigada@empresa.com",
)

with st.sidebar.expander("Configuração do servidor SMTP"):
    st.caption(
        "Dica: guarde essas credenciais em `.streamlit/secrets.toml` em vez "
        "de digitá-las toda vez. Ex.: SIAV_SMTP_HOST, SIAV_SMTP_USER, "
        "SIAV_SMTP_PASSWORD (use uma 'senha de app', não a senha normal)."
    )
    _secrets = st.secrets if hasattr(st, "secrets") else {}
    smtp_host = st.text_input("Servidor SMTP", value=_secrets.get("SIAV_SMTP_HOST", "smtp.gmail.com"))
    smtp_port = st.number_input("Porta SMTP", value=int(_secrets.get("SIAV_SMTP_PORT", 587)), step=1)
    smtp_user = st.text_input("Usuário SMTP (remetente)", value=_secrets.get("SIAV_SMTP_USER", ""))
    smtp_password = st.text_input(
        "Senha SMTP", value=_secrets.get("SIAV_SMTP_PASSWORD", ""), type="password"
    )

test_email_button = st.sidebar.button("✉️ Enviar e-mail de teste", use_container_width=True)

if test_email_button:
    if not email_destinatario or not smtp_host or not smtp_user or not smtp_password:
        st.sidebar.error("Preencha o e-mail de destino e as credenciais SMTP antes de testar.")
    else:
        _bloco_teste = _bloco_operador(operador_info)
        _mensagem_teste = (
            "Este é um e-mail de teste do SIAV-Itaqui.\n\n"
            "Se você recebeu esta mensagem, a configuração de SMTP está "
            "correta e os alertas de Microvazamento/Ruptura serão "
            "enviados normalmente para este endereço."
        )
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

st.sidebar.divider()
st.sidebar.caption(
    "Nota: durante a simulação, a tela roda de forma contínua até o fim "
    "do período escolhido. Outros controles só respondem depois que ela terminar."
)

# --- Corpo principal ---
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
        cols[0].metric("Pressão (bar)", f"{result['pressure_bar']:.2f}")
        cols[1].metric("Vazão (m³/h)", f"{result['flow_m3h']:.1f}")
        cols[2].metric("Vibração (mm/s)", f"{result['vibration_mms']:.2f}")
        cols[3].metric("Classificação do modelo", result["predicted_label"])


def render_chart(history: list[dict]):
    if not history:
        chart_placeholder.info("Clique em ▶️ Iniciar simulação na barra lateral para começar.")
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
        alert_log_placeholder.info("Nenhum alerta emitido ainda nesta simulação.")


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
        if last.email_status:
            st.caption(f"Status do e-mail: {last.email_status}")


def despachar_email_se_preciso(alert):
    """Se o alerta inclui o canal Email e o envio está ativado/configurado, dispara via SMTP e
    grava o resultado em alert.email_status (usado no log e na prévia)."""
    if "Email" not in alert.channels:
        return
    if not email_enabled:
        alert.email_status = "Desativado (ative na barra lateral)"
        return
    if not email_destinatario or not smtp_host or not smtp_user or not smtp_password:
        alert.email_status = "Não enviado (configuração incompleta)"
        return

    corpo_email = alert.messages.get("Email", "")
    # alert.messages["Email"] vem como "Assunto: ...\n\n<corpo>"; separa de novo aqui
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


# --- Estado inicial (antes de qualquer simulação rodar) ---
render_status(st.session_state.confirmed_criticality)
if st.session_state.history:
    render_metrics(st.session_state.history[-1])
    render_chart(list(st.session_state.history))
else:
    metrics_placeholder.empty()
    chart_placeholder.info("Clique em ▶️ Iniciar simulação na barra lateral para começar.")
render_alert_log()
st.subheader("Prévia das mensagens (SMS / WhatsApp simulados / E-mail)")
message_placeholder = st.empty()
render_last_alert_messages()

# --- Loop da simulação ---
if start_button and not operador_completo:
    st.error(
        "Não é possível iniciar a simulação sem a identificação do operador de turno "
        "(Nome e Matrícula) — rastreabilidade obrigatória para o relatório de SMS."
    )
elif start_button:
    st.session_state.pipeline = SIAVPipeline()
    st.session_state.history.clear()
    st.session_state.alert_center = AlertCenter()
    st.session_state.confirmed_criticality = "Nenhum"

    st.caption(
        f"Turno em operação: **{operador_info['nome']}** (matrícula {operador_info['matricula']}) "
        f"— {operador_info['turno']} — {operador_info['terminal'] or 'terminal não informado'}"
    )

    for reading in simulate_live_feed(scenario_label=scenario, berco=berco, duration_s=duration_s):
        result = st.session_state.pipeline.process_reading(reading)
        st.session_state.history.append(result)
        data_export.save_reading(st.session_state.db_conn, result)

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

    st.success("Simulação concluída.")
    st.caption(
        f"Leituras salvas em `{data_export.READINGS_CSV}` / alertas em "
        f"`{data_export.ALERTS_CSV}` (e no SQLite `{data_export.DB_PATH}`), "
        "prontos para o Looker Studio."
    )

# --- Download dos CSVs para o Looker Studio ---
# Necessário especialmente no Streamlit Cloud, onde não há acesso direto ao
# sistema de arquivos do servidor: o operador baixa aqui e depois sobe no
# Google Drive/Sheets para conectar ao Looker Studio.
st.divider()
st.subheader("📥 Exportar dados para o Looker Studio")
st.caption(
    "Baixe os CSVs atualizados e suba-os no Google Drive (ou em uma Planilha "
    "Google) para conectar ao Looker Studio."
)

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
else:
    col_dl1.info("Ainda não há leituras salvas. Rode uma simulação primeiro.")

if os.path.exists(data_export.ALERTS_CSV):
    with open(data_export.ALERTS_CSV, "rb") as f:
        col_dl2.download_button(
            "⬇️ Baixar live_alerts.csv",
            data=f.read(),
            file_name="live_alerts.csv",
            mime="text/csv",
            use_container_width=True,
        )
else:
    col_dl2.info("Ainda não há alertas salvos. Rode uma simulação com anomalia primeiro.")
