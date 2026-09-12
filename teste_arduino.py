import streamlit as st
import serial
import time

st.set_page_config(page_title="Teste de Bancada SIAV", layout="wide")

st.title("🔬 Teste de Bancada - Sensores do Arduino")
st.markdown("---")

# Configuração da Porta Serial
porta = st.sidebar.text_input("Porta COM", value="COM3")
baudrate = 115200

@st.cache_resource
def conectar(p, b):
    try:
        ser = serial.Serial(p, b, timeout=1)
        time.sleep(2)
        return ser
    except Exception as e:
        st.error(f"Erro ao abrir a porta {p}: {e}")
        return None

arduino = conectar(porta, baudrate)

if arduino:
    st.success(f"Conectado ao Arduino na porta {porta}!")
    
    col1, col2 = st.columns(2)
    metric_pressao = col1.empty()
    metric_vazao = col2.empty()
    
    st.info("Assopre o sensor de vazão ou altere a pressão para testar os dados em tempo real.")

    if st.button("Iniciar Leitura Contínua"):
        while True:
            if arduino.in_waiting > 0:
                linha = arduino.readline().decode('utf-8').strip()
                dados = linha.split(',')
                if len(dados) == 2:
                    pressao = float(dados[0])
                    vazao = float(dados[1])
                    
                    metric_pressao.metric(label="Pressão (PSI)", value=f"{pressao:.2f} PSI")
                    metric_vazao.metric(label="Vazão (L/min)", value=f"{vazao:.2f} L/min")
            time.sleep(0.5)
