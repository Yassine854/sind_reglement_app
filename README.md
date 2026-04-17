# Réglements CAM — Dashboard

## 🚀 Lancement rapide (FastAPI)

```bash
cd reglements_app
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```
Puis ouvrir **http://localhost:8000**

---

## 📋 Utilisation

1. Glisser-déposer ou sélectionner votre fichier `.txt` mensuel
2. Le dashboard affiche automatiquement :
   - **Résumé global** : total réglé, nb transactions, CAMs actives
   - **Par type** : Espèces (CESP), Traite (CTRT), Chèque (CCHQR)
   - **Classement des CAMs** : rang, montant, détail par type, part en %

## 🎯 CAMs ciblées
CAM01, CAM02, CAM03, CAM04, CAM05, CAM06, CAM36, CAM37, CAM38, CAM48, CAM49

## 📓 Google Colab
Ouvrir `analyse_reglements.ipynb` dans Google Colab pour l'analyse Python avec export CSV.
