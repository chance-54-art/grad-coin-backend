# GRAD-COIN backend - deploiement Render (manuel, depuis le telephone)

Fichiers : server.py (backend Python/aiohttp), requirements.txt, Procfile, render.yaml.

1. Sur render.com : New > Web Service > creer manuellement (ou Blueprint depuis render.yaml).
2. Runtime : Python 3.12. Build : pip install -r requirements.txt. Start : python server.py.
3. Health check : /health. Variable PORT : 10000 (Render la fournit de toute facon).
4. Deploy. L'URL publique sera https://grad-coin.onrender.com (nom du service "grad-coin").
5. Verifier : ouvrir https://grad-coin.onrender.com/health doit repondre 200.

La base SQLite est creee au premier demarrage dans le dossier du service (jetable sur Render, ok pour le test).
Le backend etait teste en local : inscription = +1000 GRAD une fois, minage 10 GRAD/h, plafond 240 par cycle de 24 h.
