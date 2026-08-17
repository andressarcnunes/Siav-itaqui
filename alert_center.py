"""
alert_center.py
================
Central de Alertas do SIAV-Itaqui — decide QUANDO um alerta deve ser
confirmado (debounce, por berço) e PARA ONDE ele deve ir, conforme a
criticidade (Baixa/Média/Crítica), incluindo o texto simulado de cada
mensagem.

Essa lógica antes estava só dentro do dashboard (Etapa 4); agora fica
centralizada aqui, então tanto o dashboard quanto qualquer script de
linha de comando usam exatamente a mesma regra -- sem risco de as duas
pontas divergirem com o tempo.

Nesta fase de hackathon, o envio de SMS/WhatsApp é SIMULADO (só
registrado em log/tela) -- a integração real ficaria a cargo de um
provedor como Twilio (SMS) ou a API oficial do WhatsApp Business durante
a fase de aceleração, conforme descrito na seção de Viabilidade do
projeto.

Uso rápido (roda uma demonstração no terminal):
    python src/alert_center.py
"""

from __future__ import annotations

from dataclasses import dataclass

DEBOUNCE_TICKS = 2  # nº de leituras consecutivas iguais para confirmar uma mudança de estado

CRITICALITY_CHANNELS = {
    "Nenhum": [],
    "Baixa": ["Dashboard"],
    "Média": ["Dashboard", "SMS"],
    "Crítica": ["Dashboard", "SMS", "WhatsApp"],
}

_ESTADO_LEGIVEL = {
    "normal": "Operação normal",
    "microvazamento": "Microvazamento",
    "ruptura_parcial": "Ruptura parcial",
    "ruptura_total": "Ruptura total",
}

# Mensagens por canal: SMS curto e direto; WhatsApp com mais contexto e
# instrução operacional; Dashboard é tratado à parte (é sempre um registro
# estruturado, não uma "mensagem" no mesmo sentido).
_TEMPLATES = {
    "Média": {
        "SMS": "SIAV-Itaqui: alerta MEDIO no berco {berco} as {hora}. Possivel {estado_fmt}. Verificar painel.",
    },
    "Crítica": {
        "SMS": "SIAV-Itaqui: ALERTA CRITICO berco {berco} as {hora}. {estado_fmt}. Acionar equipe AGORA.",
        "WhatsApp": (
            "🚨 *ALERTA CRÍTICO — SIAV-Itaqui*\n"
            "Berço: {berco}\n"
            "Horário: {hora}\n"
            "Estado detectado: {estado_fmt}\n"
            "Ação recomendada: bloqueio imediato de válvulas e acionamento "
            "da equipe de emergência no local."
        ),
    },
}


@dataclass
class DispatchedAlert:
    timestamp: object
    berco: str
    predicted_label: str
    criticality: str
    channels: list
    messages: dict  # canal -> texto da mensagem


class AlertCenter:
    """
    Mantém o estado de criticidade confirmada por berço (com debounce) e
    gera o despacho simulado de alertas para os canais corretos.

    Um objeto por sessão de monitoramento -- o dashboard cria um novo a
    cada simulação iniciada.
    """

    def __init__(self, debounce_ticks: int = DEBOUNCE_TICKS):
        self.debounce_ticks = debounce_ticks
        self._confirmed: dict = {}
        self._pending: dict = {}
        self._pending_count: dict = {}
        self.log: list = []

    def process(self, prediction: dict) -> DispatchedAlert | None:
        """
        Recebe o resultado de uma previsão do pipeline (dict com berco,
        predicted_label, criticality, timestamp) e retorna um
        DispatchedAlert SE essa leitura confirmar uma MUDANÇA de estado
        (após `debounce_ticks` leituras consecutivas iguais). Caso
        contrário, retorna None -- ainda não há alerta novo a despachar.
        """
        berco = prediction["berco"]
        raw_criticality = prediction["criticality"]

        confirmed = self._confirmed.get(berco, "Nenhum")
        pending = self._pending.get(berco, "Nenhum")
        count = self._pending_count.get(berco, 0)

        if raw_criticality == pending:
            count += 1
        else:
            pending = raw_criticality
            count = 1

        self._pending[berco] = pending
        self._pending_count[berco] = count

        if count < self.debounce_ticks or raw_criticality == confirmed:
            return None

        self._confirmed[berco] = raw_criticality
        channels = CRITICALITY_CHANNELS[raw_criticality]
        hora = prediction["timestamp"].strftime("%H:%M:%S")
        estado_fmt = _ESTADO_LEGIVEL.get(prediction["predicted_label"], prediction["predicted_label"])
        templates = _TEMPLATES.get(raw_criticality, {})

        messages = {}
        if "Dashboard" in channels:
            messages["Dashboard"] = f"[{hora}] Berço {berco}: {estado_fmt} — criticidade {raw_criticality}"
        for channel, template in templates.items():
            if channel in channels:
                messages[channel] = template.format(berco=berco, hora=hora, estado_fmt=estado_fmt)

        alert = DispatchedAlert(
            timestamp=prediction["timestamp"],
            berco=berco,
            predicted_label=prediction["predicted_label"],
            criticality=raw_criticality,
            channels=channels,
            messages=messages,
        )
        self.log.append(alert)
        return alert

    def confirmed_criticality(self, berco: str) -> str:
        return self._confirmed.get(berco, "Nenhum")


def _demo():
    """Demonstração no terminal: simula uma ruptura total e mostra os despachos."""
    from live_pipeline import SIAVPipeline, simulate_live_feed

    print("Carregando modelo e simulando cenário: ruptura_total...\n")
    pipeline = SIAVPipeline()
    center = AlertCenter()

    for reading in simulate_live_feed(scenario_label="ruptura_total", duration_s=100, seed=11):
        result = pipeline.process_reading(reading)
        alert = center.process(result)
        if alert:
            print(f"=== ALERTA DESPACHADO — criticidade {alert.criticality} ===")
            print(f"Berço {alert.berco} | estado: {alert.predicted_label} | canais: {', '.join(alert.channels)}")
            for canal, msg in alert.messages.items():
                print(f"  [{canal}] {msg}")
            print()

    print(f"Total de alertas despachados na simulação: {len(center.log)}")


if __name__ == "__main__":
    _demo()
