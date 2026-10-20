from app.services.health_service import get_health

def health_check():
    return get_health()
