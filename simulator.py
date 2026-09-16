"""
simulator.py
============
Simulador de telemetria dos berços 104 e 108 do Porto do Itaqui.

Gera séries temporais sintéticas de pressão, vazão e vibração, simulando:
  - Operação normal (com ruído realista de sensores industriais)
  - Microvazamento (queda gradual e sutil de pressão)
  - Ruptura parcial (queda moderada e súbita, com pico de vazão)
  - Ruptura total (colapso rápido de pressão, disparo de vazão e vibração)

Este é o ponto de partida do pipeline: os dados gerados aqui alimentam o
treinamento do modelo de IA (Random Forest) e, depois, o dashboard em
tempo real no Streamlit.

Uso rápido:
    python simulator.py
Isso gera um dataset em data/telemetry_dataset.csv e imprime um resumo.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass
from datetime import datetime, timedelta


# ---------------------------------------------------------------------------
# Parâmetros físicos de referência (valores plausíveis para o cenário do
# edital: operação de granéis líquidos nos berços 104 e 108).
# ---------------------------------------------------------------------------

BERCOS = ["104", "108"]

# Linha de base em operação normal.
# Calibrado conforme a Granel Química: a faixa segura acordada é de 6.0 a
# 7.0 bar, com 6.5 bar como ponto central de operação normal -- mantendo
# uma margem confortável em relação ao limite de alarme crítico (8.0 bar,
# ver CRITICALITY_MAP / live_pipeline.py).
BASELINE_PRESSURE_BAR = 6.5
BASELINE_FLOW_M3H = 150.0
BASELINE_VIBRATION_MMS = 0.8

# Faixa segura de operação normal (bar), usada para "grampear" (clip) a
# pressão simulada em operação normal e evitar que o ruído gaussiano, por
# acaso, gere leituras fora da faixa acordada com a operação.
NORMAL_PRESSURE_MIN_BAR = 6.0
NORMAL_PRESSURE_MAX_BAR = 7.0

# Ruído (desvio padrão) de sensores em operação normal
NOISE_PRESSURE = 0.05
NOISE_FLOW = 2.0
NOISE_VIBRATION = 0.05

LABELS = ["normal", "microvazamento", "ruptura_parcial", "ruptura_total"]


@dataclass
class ScenarioConfig:
    """Configuração de um cenário de simulação."""
    label: str
    duration_s: int
    anomaly_start_frac: float = 0.4  # em que ponto da janela a anomalia começa (0 a 1)


def _normal_series(n: int, rng: np.random.Generator) -> dict:
    """
    Gera n leituras de operação normal, com ruído gaussiano realista.

    A pressão é grampeada (np.clip) na faixa segura acordada com a
    operação (6.0 a 7.0 bar) -- assim a simulação de "operação normal"
    nunca produz, por acaso, uma leitura de pressão fora da faixa
    estável real, mantendo-a longe do limiar de alarme crítico.
    """
    pressure = BASELINE_PRESSURE_BAR + rng.normal(0, NOISE_PRESSURE, n)
    pressure = np.clip(pressure, NORMAL_PRESSURE_MIN_BAR, NORMAL_PRESSURE_MAX_BAR)
    flow = BASELINE_FLOW_M3H + rng.normal(0, NOISE_FLOW, n)
    vibration = BASELINE_VIBRATION_MMS + rng.normal(0, NOISE_VIBRATION, n)
    return {"pressure_bar": pressure, "flow_m3h": flow, "vibration_mms": vibration}


def _inject_microvazamento(data: dict, start_idx: int, rng: np.random.Generator) -> dict:
    """
    Microvazamento: queda LENTA e sutil de pressão (difícil de perceber a
    olho nu), com leve aumento de vazão (o líquido escapando) e vibração
    praticamente inalterada. É o cenário mais difícil de detectar --
    exatamente o que o modelo precisa aprender a pegar cedo.
    """
    n = len(data["pressure_bar"])
    tail = n - start_idx
    # Queda gradual e progressiva (rampa suave, não abrupta)
    ramp = np.linspace(0, 0.01 * tail, tail)  # até ~1% de queda acumulada por leitura
    data["pressure_bar"][start_idx:] -= ramp + rng.normal(0, NOISE_PRESSURE, tail)
    data["flow_m3h"][start_idx:] += np.linspace(0, 3.0, tail) + rng.normal(0, NOISE_FLOW, tail)
    data["vibration_mms"][start_idx:] += rng.normal(0, NOISE_VIBRATION * 1.2, tail)
    return data


def _inject_ruptura_parcial(data: dict, start_idx: int, rng: np.random.Generator) -> dict:
    """
    Ruptura parcial: queda moderada e relativamente rápida de pressão,
    aumento perceptível de vazão (perda de carga) e leve aumento de
    vibração na tubulação/junta afetada.
    """
    n = len(data["pressure_bar"])
    tail = n - start_idx
    drop = np.linspace(0, 1.8, min(tail, 8))
    if tail > len(drop):
        drop = np.concatenate([drop, np.full(tail - len(drop), 1.8)])
    data["pressure_bar"][start_idx:] -= drop[:tail] + rng.normal(0, NOISE_PRESSURE, tail)
    data["flow_m3h"][start_idx:] += np.linspace(0, 12.0, tail) + rng.normal(0, NOISE_FLOW, tail)
    data["vibration_mms"][start_idx:] += np.linspace(0, 0.6, tail) + rng.normal(0, NOISE_VIBRATION, tail)
    return data


def _inject_ruptura_total(data: dict, start_idx: int, rng: np.random.Generator) -> dict:
    """
    Ruptura total: colapso rápido de pressão (poucos segundos), disparo
    de vazão e pico forte de vibração. É o cenário mais fácil de
    detectar, mas também o mais crítico -- exige alerta imediato.
    """
    n = len(data["pressure_bar"])
    tail = n - start_idx
    collapse = np.linspace(0, BASELINE_PRESSURE_BAR - 0.5, min(tail, 4))
    if tail > len(collapse):
        collapse = np.concatenate([collapse, np.full(tail - len(collapse), BASELINE_PRESSURE_BAR - 0.5)])
    data["pressure_bar"][start_idx:] -= collapse[:tail] + rng.normal(0, NOISE_PRESSURE * 2, tail)
    data["flow_m3h"][start_idx:] += np.linspace(0, 35.0, tail) + rng.normal(0, NOISE_FLOW * 1.5, tail)
    data["vibration_mms"][start_idx:] += np.linspace(0, 2.2, tail) + rng.normal(0, NOISE_VIBRATION * 2, tail)
    return data


_INJECTORS = {
    "microvazamento": _inject_microvazamento,
    "ruptura_parcial": _inject_ruptura_parcial,
    "ruptura_total": _inject_ruptura_total,
}


def generate_scenario(
    scenario: ScenarioConfig,
    berco: str,
    start_time: datetime,
    rng: np.random.Generator,
    sample_interval_s: int = 1,
) -> pd.DataFrame:
    """
    Gera uma sequência temporal de leituras para um cenário específico.

    Para cenários de anomalia, a primeira parte da janela é operação
    normal e, a partir de `anomaly_start_frac`, a anomalia é injetada
    progressivamente -- simulando a evolução real de um vazamento.
    """
    n = scenario.duration_s // sample_interval_s
    data = _normal_series(n, rng)

    labels = np.array(["normal"] * n, dtype=object)

    if scenario.label != "normal":
        start_idx = int(n * scenario.anomaly_start_frac)
        data = _INJECTORS[scenario.label](data, start_idx, rng)
        labels[start_idx:] = scenario.label

    timestamps = [start_time + timedelta(seconds=i * sample_interval_s) for i in range(n)]

    df = pd.DataFrame({
        "timestamp": timestamps,
        "berco": berco,
        "pressure_bar": np.round(data["pressure_bar"], 3),
        "flow_m3h": np.round(data["flow_m3h"], 2),
        "vibration_mms": np.round(data["vibration_mms"], 3),
        "label": labels,
    })
    return df


def build_training_dataset(
    n_sequences_per_scenario: int = 40,
    duration_s: int = 120,
    seed: int = 42,
) -> pd.DataFrame:
    """
    Monta o dataset completo de treinamento, combinando várias sequências
    de cada cenário (normal, microvazamento, ruptura parcial, ruptura
    total), alternando entre os berços 104 e 108.

    Retorna um único DataFrame ordenado por tempo, pronto para virar
    features no próximo passo (treinamento do modelo).
    """
    rng = np.random.default_rng(seed)
    all_frames = []
    current_time = datetime(2026, 8, 8, 6, 0, 0)

    scenarios = [
        ScenarioConfig(label="normal", duration_s=duration_s),
        ScenarioConfig(label="microvazamento", duration_s=duration_s, anomaly_start_frac=0.3),
        ScenarioConfig(label="ruptura_parcial", duration_s=duration_s, anomaly_start_frac=0.5),
        ScenarioConfig(label="ruptura_total", duration_s=duration_s, anomaly_start_frac=0.6),
    ]

    for scenario in scenarios:
        for i in range(n_sequences_per_scenario):
            berco = BERCOS[i % len(BERCOS)]
            df = generate_scenario(scenario, berco, current_time, rng)
            all_frames.append(df)
            current_time += timedelta(seconds=duration_s + 5)  # pequeno intervalo entre sequências

    dataset = pd.concat(all_frames, ignore_index=True)
    dataset = dataset.sort_values("timestamp").reset_index(drop=True)
    return dataset


def main():
    dataset = build_training_dataset()

    output_path = "/home/claude/siav-itaqui/data/telemetry_dataset.csv"
    dataset.to_csv(output_path, index=False)

    print(f"Dataset gerado: {len(dataset)} leituras")
    print(f"Salvo em: {output_path}")
    print("\nDistribuição de rótulos:")
    print(dataset["label"].value_counts())
    print("\nAmostra dos dados:")
    print(dataset.head(10).to_string(index=False))


if __name__ == "__main__":
    main()
