from telephony.call_manager import call_manager

def get_gsm_status() -> dict:
    return {
        "sinal": call_manager.get_signal(),
        "registro": call_manager.get_registration(),
        "ligacao_ativa": call_manager.call_active,
        "numero_chamando": call_manager.incoming_number,
    }
