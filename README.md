# Analyse Règlements CAM

## Structure
```
reglement_app/
├── main.py            ← FastAPI backend
├── requirements.txt
└── static/
    └── index.html     ← Frontend (HTML + CSS + JS)
```

## Lancement

```bash
# 1. Installer les dépendances
pip install -r requirements.txt

# 2. Lancer le serveur (depuis le dossier reglement_app/)
uvicorn main:app --reload

# 3. Ouvrir dans le navigateur
# http://localhost:8000
```

## Utilisation
- Glisser-déposer ou cliquer pour uploader votre fichier .txt mensuel
- Le fichier est analysé côté serveur (FastAPI)
- 3 onglets : Classement / Détail par type / Opérations

## CAMs ciblées
cam01, cam02, cam03, cam04, cam05, cam06, cam36, cam37, cam38, cam48, cam49

## Types de règlement détectés
- CESP... → Espèces
- CTRT... → Traite
- CCHQR... → Chèque
