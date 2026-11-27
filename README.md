# GRAD-COIN - app Android native

Application de minage GRAD-COIN (Kotlin, noir et or, interface en francais).
Le solde et le minage sont calcules cote serveur (Python/aiohttp, server/server.py).
Aucun minage local, aucun solde invente dans l'app.

Regles serveur verifiees par tests (voir server/) :
- bienvenue : +1000 GRAD, une seule fois par compte/appareil
- minage : 10 GRAD/heure, plafond 240 par cycle de 24h, pas d'energie qui expire
- recompense quotidienne, serie (streak), combo + cipher, ligues, ameliorations
- parrainage : bonus referent + filleul, paliers dans /api/me
- portefeuille BNB (adresse seulement), profil

## Fichiers
- app/ : projet Android (compileSdk 34, minSdk 26 -> compatible Galaxy S9 Android 8+)
- server/server.py : backend, base SQLite, POST /api/{action}, GET /health
- app/build/outputs/apk/debug/app-debug.apk : APK de test (a installer sur le telephone)

## Compiler
  ANDROID_HOME=/tmp/android-sdk ./gradlew assembleDebug

## Lancer le serveur (Render / VPS)
  cd server && pip install -r requirements.txt
  PORT=10000 python3 server.py
Puis mettre l'URL https dans gradle.properties (api.base.url) avant de compiler.
