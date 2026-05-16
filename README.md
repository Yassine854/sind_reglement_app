# Réglements CAM — Dashboard

## 🚀 Lancement rapide (FastAPI)

```bash
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```
Puis ouvrir **http://localhost:8000**

---

## 📋 Utilisation

1. Importez votre **fichier du mois courant** (`REGLEMENT.txt`) puis vos **fichiers historiques** (`.txt`) depuis l'interface.
2. Les données importées sont conservées côté application pendant **7 jours** (et restent disponibles après refresh pendant cette période).
3. Un bandeau en haut affiche la **période couverte** par les fichiers chargés (mois/année + dates exactes).
4. Si vous réimportez des historiques qui couvrent une période déjà stockée, les données existantes qui se chevauchent sont remplacées par le nouvel import.
5. Vous pouvez filtrer une période avec les champs **Du / Au** :
   - le filtre s'applique sur l'ensemble des données stockées (mensuel + historiques)
   - la vue par défaut reste basée sur le fichier mensuel chargé
6. Le dashboard affiche automatiquement :
   - **Résumé global** : total réglé, nb transactions, CAMs actives
   - **Par type** : Espèces (CESP), Traite (CTRT), Chèque (CCHQR)
   - **Classement des CAMs** : rang, montant, site d'appartenance, répartition par type
   - **Par site** : totaux regroupés par site (SFX, MAH, NAB, SSE, TUN), y compris les lignes sans CAM explicite si le site est présent
7. L'import manuel d'un fichier `.txt` (endpoint `/upload`) reste disponible si besoin.

### Variables d'environnement optionnelles

Pour tester l'application sur une autre machine ou avec des fichiers temporaires, vous pouvez surcharger les chemins par défaut :

- `REGLEMENT_CURRENT_FILE`
- `REGLEMENT_HISTORY_DIR`

## 🗺️ Sites et CAMs

| Site | CAMs |
|------|------|
| **SFX** | CAM01, CAM02, CAM03, CAM04, CAM05, CAM06, CAM07, CAM36, CAM37, CAM38, CAM48, CAM49, CAM58, CAM59 |
| **MAH** | CAM40, CAM41, CAM42, CAM43, CAM44, CAM45, CAM57 |
| **NAB** | CAM50, CAM51, CAM52, CAM53, CAM54 |
| **SSE** | CAM08, CAM09, CAM10, CAM11, CAM12, CAM13, CAM14, CAM15, CAM39, CAM46, CAM47 |
| **TUN** | CAM16, CAM17, CAM18, CAM19, CAM20, CAM21, CAM22, CAM23, CAM24, CAM25, CAM26, CAM27, CAM29, CAM30, CAM31 |

Les CAMs non répertoriées sont classées comme **Inconnu**.

## 📓 Google Colab
Ouvrir `analyse_reglements.ipynb` dans Google Colab pour l'analyse Python avec export CSV.
