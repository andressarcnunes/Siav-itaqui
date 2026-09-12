import streamlit as st
import serial
import serial.tools.list_ports
import pandas as pd
import numpy as np
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

st.set_page_config(
    page_title="SIAV-Itaqui - Monitoramento",
    page_icon="📊",
    layout="wide"
)

def listar_portas_com():
    portas = serial.tools.list_ports.comports()
    return [porta.device for porta in portas]

def enviar_email_alerta(destinatario, assunto, mensagem, smtp_host, smtp_port, smtp_user, smtp_password):
    try:
        msg = MIMEMultipart()
        msg['From'] = smtp_user
        msg['To'] = destinatario
        msg['Subject'] = assunto
        msg.attach(MIMEText(mensagem, 'plain'))

        server = smtplib.SMTP(smtp_host, int(smtp_port))
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, destinatario, msg.as_string())
        server.quit()
        return True, ""
    except Exception as e:
        return False, str(e)

def bloco_operador(info):
    if not info:
        return ""
    return f"Informações do Operador / Bancada:\n{info}"

st.sidebar.title("Painel de Controle")
st.sidebar.markdown("---")

st.sidebar.subheader("🔌 Conexão Serial (Arduino)")
portas_disponiveis = listar_portas_com()
porta_selecionada = st.sidebar.selectbox("Selecione a Porta COM", portas_disponiveis if portas_disponiveis else ["COM4"])
baudrate = st.sidebar.selectbox("Baudrate", [9600, 115200], index=0)

conectar_btn = st.sidebar.button("Conectar Sensor")

st.sidebar.markdown("---")
st.sidebar.subheader("👤 Identificação")
operador_info = st.sidebar.text_input("Nome do Operador / Observações", "Operador Padrão - Bancada Itaqui")

st.sidebar.markdown("---")
st.sidebar.subheader("✉️ Alertas por e-mail")

email_enabled = st.sidebar.checkbox("Ativar envio de e-mail em alertas", value=False)
email_destinatario = st.sidebar.text_input("E-mail da brigada/gestão")

with st.sidebar.expander("Configuração do servidor SMTP"):
    _secrets = st.secrets if hasattr(st, "secrets") else {}
    smtp_host = st.text_input("Servidor SMTP", value=_secrets.get("SIAV_SMTP_HOST", "smtp.gmail.com"))
    smtp_port = st.number_input("Porta SMTP", value=int(_secrets.get("SIAV_SMTP_PORT", 587)), step=1)
    smtp_user = st.text_input("Usuário SMTP (remetente)", value=_secrets.get("SIAV_SMTP_USER", ""))
    smtp_password = st.text_input("Senha SMTP", value=_secrets.get("SIAV_SMTP_PASSWORD", ""), type="password")

test_email_button = st.sidebar.button(" ✉️ Enviar e-mail de teste")

if test_email_button:
    if not email_destinatario or not smtp_host or not smtp_user or not smtp_password:
        st.sidebar.error("Preencha o e-mail de destino e as credenciais SMTP antes de testar.")
    
    bloco_teste = bloco_operador(operador_info)
    mensagem_teste = "Este é um e-mail de teste do SIAV-Itaqui."
    if bloco_teste:
        mensagem_teste = f"{mensagem_teste}\n\n{bloco_teste}"

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
        st.sidebar.error(f"Falha ao enviar e-mail: {erro}")

st.title("🌊 SIAV-Itaqui - Sistema de Monitoramento de Vazão e Pressão")
st.markdown("Interface local para coleta de dados via Arduino.")

col1, col2 = st.columns(2)

with col1:
    st.metric(label="Pressão Atual", value="0.00 Bar", delta="0.0 Bar")

with col2:
    st.metric(label="Vazão Atual", value="0.00 L/min", delta="0.0 L/min")

st.markdown("---")
st.info("Utilize a barra lateral para selecionar a porta COM4 correspondente ao Arduino e iniciar o monitoramento.")
