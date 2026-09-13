"""
teste_sensores_local.py
========================
Script de bancada para testar os sensores (pressao/vazao) ligados no
Arduino, SEM depender de scikit-learn/pandas -- so usa a biblioteca
padrao do Python + pyserial. Isso funciona em QUALQUER Python, inclusive
32-bit (o computador da bancada nao suporta 64-bit, entao o pipeline
completo com Random Forest nao roda localmente nele).

A classificacao aqui e por REGRAS DE LIMIAR (threshold), nao pelo
Random Forest -- e a mesma ideia que o mentor Emmanuel levantou (será
que precisa de Random Forest, ou threshold já resolve?). Serve tambem
como comparacao entre as duas abordagens mais pra frente.

Uso:
    pip install pyserial
    python teste_sensores_local.py

O script pergunta a porta serial (ou detecta automaticamente), le
continuamente, classifica por threshold e grava tudo em
bench_test_log.csv nesta mesma pasta.

Pressione Ctrl+C para parar.
"""

from __future__ import annotations

import csv
import os
import sys
import time
from datetime import datetime

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    print("Falta instalar o pyserial. Rode: pip install pyserial")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Configuracao -- ajuste esses valores conforme a bancada for calibrada
# ---------------------------------------------------------------------------
BAUDRATE_PADRAO = 9600          # tem que bater com o Serial.begin(...) do sketch
PRESSAO_BASELINE_BAR = 8.0      # pressao "normal" de referencia (ajuste pro seu teste de bancada)

# Quanto a pressao pode cair (em relacao ao baseline) antes de mudar de estado.
# Sao os mesmos 4 estados que o modelo de IA usa, só que decididos por regra
# simples em vez de Random Forest.
LIMIAR_MICROVAZAMENTO_BAR = 0.5   # queda pequena -> Baixa criticidade
LIMIAR_RUPTURA_PARCIAL_BAR = 1.5  # queda moderada -> Media criticidade
LIMIAR_RUPTURA_TOTAL_BAR = 3.0    # queda grande -> Critica

DEBOUNCE_LEITURAS = 3  # nº de leituras seguidas no mesmo estado p/ confirmar mudança (evita flicker por ruído)

ARDUINO_RESET_DELAY_S = 2.0
LEITURA_TIMEOUT_SEM_DADOS_S = 8.0

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bench_test_log.csv")

ESTADOS_LEGIVEIS = {
    "normal": "Normal",
    "microvazamento": "Microvazamento (Baixa)",
    "ruptura_parcial": "Ruptura parcial (Média)",
    "ruptura_total": "Ruptura total (Crítica)",
}


def classificar_por_limiar(pressao_bar: float) -> str:
    """
    Classifica o estado atual comparando a queda de pressão em relação ao
    baseline com os limiares configurados acima. Regra simples e explicável
    -- sem caixa-preta, dá pra justificar cada corte pra banca.
    """
    queda = PRESSAO_BASELINE_BAR - pressao_bar
    if queda >= LIMIAR_RUPTURA_TOTAL_BAR:
        return "ruptura_total"
    if queda >= LIMIAR_RUPTURA_PARCIAL_BAR:
        return "ruptura_parcial"
    if queda >= LIMIAR_MICROVAZAMENTO_BAR:
        return "microvazamento"
    return "normal"


def portas_seriais_disponiveis():
    try:
        return [p.device for p in list_ports.comports()]
    except Exception:
        return []


def escolher_porta() -> str:
    portas = portas_seriais_disponiveis()
    if not portas:
        return input("Nenhuma porta detectada automaticamente. Digite a porta (ex.: COM4): ").strip()

    print("Portas seriais detectadas:")
    for i, p in enumerate(portas, start=1):
        print("  " + str(i) + ") " + p)
    print("  " + str(len(portas) + 1) + ") Digitar manualmente")

    escolha = input("Escolha o número da porta: ").strip()
    try:
        indice = int(escolha)
        if 1 <= indice <= len(portas):
            return portas[indice - 1]
    except ValueError:
        pass
    return input("Digite a porta (ex.: COM4): ").strip()


def abrir_conexao(porta: str, baudrate: int) -> serial.Serial:
    conexao = serial.Serial(porta, baudrate=baudrate, timeout=2)
    # O Arduino Uno reinicia quando a porta serial é aberta. Espera o
    # reset terminar antes de limpar o buffer, senão a gente lê lixo de boot.
    time.sleep(ARDUINO_RESET_DELAY_S)
    conexao.reset_input_buffer()
    return conexao


def preparar_csv():
    novo_arquivo = not os.path.exists(CSV_PATH)
    arquivo = open(CSV_PATH, "a", newline="", encoding="utf-8")
    escritor = csv.writer(arquivo)
    if novo_arquivo:
        escritor.writerow(["timestamp", "pressao_bar", "vazao_m3h", "estado_bruto", "estado_confirmado"])
    return arquivo, escritor


def main():
    print("=== Teste local de sensores — SIAV-Itaqui (sem scikit-learn/pandas) ===\n")
    porta = escolher_porta()
    baudrate_input = input("Baudrate (Enter para usar " + str(BAUDRATE_PADRAO) + "): ").strip()
    baudrate = int(baudrate_input) if baudrate_input else BAUDRATE_PADRAO

    print("\nAbrindo porta " + porta + " a " + str(baudrate) + " baud...")
    try:
        conexao = abrir_conexao(porta, baudrate)
    except serial.SerialException as exc:
        print("Erro ao abrir a porta: " + str(exc))
        print(
            "Verifique se: (1) o Arduino está conectado; (2) a porta está certa; "
            "(3) o Monitor Serial da Arduino IDE está fechado."
        )
        return

    arquivo_csv, escritor_csv = preparar_csv()
    print("Gravando leituras em: " + CSV_PATH)
    print("Pressione Ctrl+C para parar.\n")
    print("{:>10} | {:>8} | {:>8} | {:>24} | {:>24}".format(
        "hora", "pressão", "vazão", "estado (bruto)", "estado (confirmado)"
    ))
    print("-" * 90)

    estado_confirmado = "normal"
    estado_pendente = "normal"
    contagem_pendente = 0
    ultimo_dado_valido = time.time()

    try:
        while True:
            try:
                linha_bruta = conexao.readline().decode("utf-8", errors="ignore").strip()
            except serial.SerialException as exc:
                print("Conexão perdida: " + str(exc))
                break

            if not linha_bruta:
                if time.time() - ultimo_dado_valido > LEITURA_TIMEOUT_SEM_DADOS_S:
                    print("(!) Nenhum dado válido há mais de "
                          + str(int(LEITURA_TIMEOUT_SEM_DADOS_S)) + "s — confira a fiação/sketch.")
                    ultimo_dado_valido = time.time()
                continue

            partes = linha_bruta.split(",")
            if len(partes) != 2:
                # linha fora do formato esperado (pressao,vazao) -- ignora
                continue

            try:
                pressao_bar = float(partes[0].strip())
                vazao_m3h = float(partes[1].strip())
            except ValueError:
                continue

            ultimo_dado_valido = time.time()
            estado_bruto = classificar_por_limiar(pressao_bar)

            # Debounce simples: só confirma mudança de estado após N leituras seguidas iguais
            if estado_bruto == estado_pendente:
                contagem_pendente += 1
            else:
                estado_pendente = estado_bruto
                contagem_pendente = 1

            if contagem_pendente >= DEBOUNCE_LEITURAS:
                estado_confirmado = estado_pendente

            agora = datetime.now()
            print("{:>10} | {:>8.3f} | {:>8.2f} | {:>24} | {:>24}".format(
                agora.strftime("%H:%M:%S"),
                pressao_bar,
                vazao_m3h,
                ESTADOS_LEGIVEIS[estado_bruto],
                ESTADOS_LEGIVEIS[estado_confirmado],
            ))

            escritor_csv.writerow([
                agora.isoformat(), pressao_bar, vazao_m3h, estado_bruto, estado_confirmado
            ])
            arquivo_csv.flush()

    except KeyboardInterrupt:
        print("\nTeste interrompido pelo usuário.")
    finally:
        try:
            conexao.close()
        except Exception:
            pass
        arquivo_csv.close()
        print("Log salvo em: " + CSV_PATH)


if __name__ == "__main__":
    main()
