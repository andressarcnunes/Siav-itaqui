"""
data_export.py
===============
Persistência do histórico de leituras e alertas do SIAV-Itaqui, para uso
em relatórios de gestão (Google Looker Studio).

Duas saídas, geradas em paralelo a cada leitura/alerta:
  - Banco SQLite local (data/siav_live.db) -- bom para consultas SQL e
    para servir de fonte a um pequeno serviço/API depois.
  - Arquivos CSV (data/live_readings.csv e data/live_alerts.csv) -- mais
    simples de sincronizar com uma planilha Google e plugar direto no
    Looker Studio (ver instruções no final deste arquivo, em comentário).

Uso típico (dentro do loop do Streamlit):
    conn = get_connection()
    init_db(conn)
    save_reading(conn, result)
    save_alert(conn, alert)          # só quando houver um alerta novo
"""

from __future__ import annotations

import csv
import os
import sqlite3
from datetime import datetime

DATA_DIR = "data"
DB_PATH = os.path.join(DATA_DIR, "siav_live.db")
READINGS_CSV = os.path.join(DATA_DIR, "live_readings.csv")
ALERTS_CSV = os.path.join(DATA_DIR, "live_alerts.csv")

READINGS_CSV_HEADER = [
    "timestamp", "berco", "pressure_bar", "flow_m3h", "vibration_mms",
    "predicted_label", "criticality",
]
# operador_nome / operador_matricula / turno / terminal: rastreabilidade do
# operador de turno responsável pelo disparo, exigida no relatório de SMS.
ALERTS_CSV_HEADER = [
    "timestamp", "berco", "predicted_label", "criticality", "channels", "email_status",
    "operador_nome", "operador_matricula", "turno", "terminal",
]


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Abre (e cria, se necessário) o banco SQLite local."""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Cria as tabelas 'leituras' e 'alertas', se ainda não existirem."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS leituras (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            berco TEXT NOT NULL,
            pressure_bar REAL,
            flow_m3h REAL,
            vibration_mms REAL,
            predicted_label TEXT,
            criticality TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alertas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            berco TEXT NOT NULL,
            predicted_label TEXT,
            criticality TEXT,
            channels TEXT,
            email_status TEXT,
            operador_nome TEXT,
            operador_matricula TEXT,
            turno TEXT,
            terminal TEXT
        )
        """
    )
    conn.commit()
    _migrar_colunas_operador(conn)


def _migrar_colunas_operador(conn: sqlite3.Connection) -> None:
    """
    Garante que bancos SQLite criados ANTES desta atualização (sem as
    colunas de operador) recebam as colunas novas via ALTER TABLE, em vez
    de exigir apagar o data/siav_live.db antigo.
    """
    colunas_existentes = {row[1] for row in conn.execute("PRAGMA table_info(alertas)")}
    colunas_novas = {
        "operador_nome": "TEXT",
        "operador_matricula": "TEXT",
        "turno": "TEXT",
        "terminal": "TEXT",
    }
    for nome, tipo in colunas_novas.items():
        if nome not in colunas_existentes:
            conn.execute(f"ALTER TABLE alertas ADD COLUMN {nome} {tipo}")
    conn.commit()


def save_reading(conn: sqlite3.Connection, result: dict) -> None:
    """Grava uma leitura/previsão no SQLite e faz append no CSV correspondente."""
    ts = result["timestamp"]
    ts_str = ts.isoformat() if isinstance(ts, datetime) else str(ts)

    conn.execute(
        """
        INSERT INTO leituras (timestamp, berco, pressure_bar, flow_m3h, vibration_mms,
                               predicted_label, criticality)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ts_str, result["berco"], result["pressure_bar"], result["flow_m3h"],
            result["vibration_mms"], result["predicted_label"], result["criticality"],
        ),
    )
    conn.commit()

    _append_csv_row(
        READINGS_CSV, READINGS_CSV_HEADER,
        [ts_str, result["berco"], result["pressure_bar"], result["flow_m3h"],
         result["vibration_mms"], result["predicted_label"], result["criticality"]],
    )


def save_alert(conn: sqlite3.Connection, alert, email_status: str = "") -> None:
    """
    Grava um alerta despachado no SQLite e faz append no CSV correspondente,
    incluindo a identificação do operador de turno responsável (lida
    diretamente de alert.operador_nome / operador_matricula / turno /
    terminal, preenchidos pelo AlertCenter.process quando o app.py passa o
    dict `operador`).
    """
    ts = alert.timestamp
    ts_str = ts.isoformat() if isinstance(ts, datetime) else str(ts)
    channels_str = ", ".join(alert.channels)
    status = email_status or getattr(alert, "email_status", "") or ""

    operador_nome = getattr(alert, "operador_nome", "") or ""
    operador_matricula = getattr(alert, "operador_matricula", "") or ""
    turno = getattr(alert, "turno", "") or ""
    terminal = getattr(alert, "terminal", "") or ""

    conn.execute(
        """
        INSERT INTO alertas (timestamp, berco, predicted_label, criticality, channels, email_status,
                              operador_nome, operador_matricula, turno, terminal)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ts_str, alert.berco, alert.predicted_label, alert.criticality, channels_str, status,
            operador_nome, operador_matricula, turno, terminal,
        ),
    )
    conn.commit()

    _append_csv_row(
        ALERTS_CSV, ALERTS_CSV_HEADER,
        [
            ts_str, alert.berco, alert.predicted_label, alert.criticality, channels_str, status,
            operador_nome, operador_matricula, turno, terminal,
        ],
    )


def _append_csv_row(path: str, header: list[str], row: list) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    is_new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(header)
        writer.writerow(row)


# ---------------------------------------------------------------------------
# Como conectar isso ao Google Looker Studio
# ---------------------------------------------------------------------------
# O Looker Studio não lê um arquivo SQLite local nem uma pasta do seu
# computador diretamente -- ele precisa de uma fonte "na nuvem". Três
# caminhos, do mais simples ao mais robusto:
#
# 1) CSV -> Google Sheets -> Looker Studio (RECOMENDADO para o hackathon)
#    a. Suba data/live_readings.csv e data/live_alerts.csv para uma
#       planilha Google (manualmente, ou automatizado com o script
#       sync_to_sheets.py incluído junto a este pacote).
#    b. No Looker Studio: "Criar" -> "Fonte de dados" -> conector nativo
#       "Planilhas Google" -> selecione a planilha e a aba.
#    c. Monte os gráficos/relatórios normalmente. Para "tempo real", basta
#       rodar sync_to_sheets.py periodicamente (ex.: a cada leitura, ou
#       em um cron a cada minuto) e clicar em "Atualizar" no relatório.
#
# 2) CSV -> Google Drive (upload direto) -> Looker Studio
#    a. Suba o CSV para o Google Drive (manual ou via API do Drive).
#    b. No Looker Studio, use o conector "Arquivo" apontando para o CSV
#       no Drive. Mais simples que o passo 1, mas exige reupload a cada
#       atualização (sem sincronização incremental).
#
# 3) SQLite -> API própria -> Looker Studio (community connector)
#    Para um cenário mais "de produção": exponha os dados do SQLite via
#    uma API HTTP simples (ex.: FastAPI/Flask lendo data/siav_live.db) e
#    escreva um "Community Connector" do Looker Studio (Apps Script) que
#    consome essa API. É mais trabalho de configuração; vale a pena só se
#    o volume de dados crescer muito ou você quiser dashboards realmente
#    ao vivo (auto-refresh) em vez de "atualizar manualmente".
#
# Para a fase de hackathon, o caminho (1) é o que dá melhor resultado com
# menos esforço.
