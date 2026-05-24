# Réglements CAM — Dashboard

## 🚀 Lancement rapide (FastAPI)

```bash
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```
Puis ouvrir **http://localhost:8000**

---

## 📋 Utilisation

1. Importez le dossier racine **Fichiers Sources** depuis l'interface.
   - L'application recherche automatiquement `REGLEMENT.txt` (mois courant).
   - L'application recherche automatiquement le sous-dossier `Réglements` (historique) et ignore les fichiers non pertinents.
2. Les données sont chargées en mémoire après import pour éviter une relecture à chaque filtre.
3. Vous pouvez filtrer une période avec les champs **Du / Au** :
   - le filtre s'applique sur l'ensemble des données stockées (mensuel + historiques)
   - la vue par défaut reste basée sur le fichier mensuel chargé
4. Le dashboard affiche automatiquement :
   - **Résumé global** : total réglé, nb transactions, CAMs actives
   - **Par type** : Espèces (CESP), Traite (CTRT), Chèque (CCHQR)
   - **Classement des CAMs** : rang, montant, site d'appartenance, répartition par type
   - **Par site** : totaux regroupés par site (SFX, MAH, NAB, SSE, TUN), y compris les lignes sans CAM explicite si le site est présent
### Variables d'environnement optionnelles

Le mode principal est l'import du dossier. En secours, vous pouvez encore définir des chemins backend :

- `CURRENT_REGLEMENT_FILE`
- `HISTORY_REGLEMENTS_DIR`
- `FILE_URI_MOUNT_ROOT` (optionnel sur Linux/macOS pour mapper `file://serveur/...` vers un point de montage local)

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
