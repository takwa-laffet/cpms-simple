# Test borne CPMS - Collecte maximale des informations

Ce document sert de guide de test pour une borne OCPP connectée au projet `cpms-simple`.

Le but est de vérifier tout ce que le CPMS peut collecter dans `/api/cp`, y compris les données en temps réel, les sessions, les logs, les métriques d'énergie, les métadonnées statiques et les commandes distantes.

## 1. Ce que le projet collecte déjà

Le projet collecte déjà les événements suivants:

- `BootNotification`
- `Heartbeat`
- `Authorize`
- `StatusNotification`
- `MeterValues`
- `StartTransaction`
- `StopTransaction`
- `DataTransfer`
- `DiagnosticsStatusNotification`
- `FirmwareStatusNotification`
- Connexion / déconnexion websocket

Les données sont visibles dans:

- `GET /api/cp`
- `GET /api/cp?cp_id=<borne>`
- `GET /api/cp/<cp_id>/meta`
- `POST /api/cp/<cp_id>/meta`
- `POST /api/cp/<cp_id>/force_start`
- `POST /api/cp/<cp_id>/force_stop`

## 2. Interprétation du log Heartbeat reçu

Exemple reçu:

```text
2026-05-21 14:50:34,412 | INFO | CP001: send [3,"1626456126",{"currentTime":"2026-05-21T14:50:34.412214+00:00"}]
```

Cela signifie:

- la borne `CP001` est connectée,
- elle a envoyé une réponse OCPP de type `Heartbeat`,
- le CPMS a renvoyé une réponse valide avec `currentTime`,
- la borne est vivante et la communication websocket fonctionne.

## 3. Objectif de collecte dans `/api/cp`

Le point `/api/cp` doit permettre de voir le maximum d'informations possibles:

1. Informations générales de la borne
2. État temps réel
3. Connecteurs
4. Sessions de recharge
5. Authentification utilisateur
6. Données énergétiques
7. Données financières
8. Alertes et maintenance
9. Commandes distantes
10. Smart charging / energy management
11. ESG / CO2
12. Données géographiques
13. Analytics
14. OCPP 2.0.1

## 4. Tests possibles par catégorie

### 4.1 Informations générales de la borne

Tester les champs suivants:

- Charge Point ID
- Fabricant
- Modèle
- Numéro de série
- Firmware version
- Adresse IP / réseau
- SIM ICCID / IMSI
- Date de mise en service
- Uptime
- Statut de connexion au CPMS
- Redémarrages / reboot logs

#### Comment tester

1. Connecter la borne au websocket.
2. Envoyer `BootNotification`.
3. Lire la réponse dans `/api/cp`.
4. Enregistrer les métadonnées statiques via:

```bash
POST /api/cp/CP001/meta
```

#### Exemple JSON de métadonnées

```json
{
  "manufacturer": "ACME",
  "model": "X2",
  "serialNumber": "SN-0001",
  "firmwareVersion": "1.0.12",
  "ipAddress": "192.168.1.20",
  "networkType": "4G",
  "iccid": "8932xxxxxxxxxxxx",
  "imsi": "208xxxxxxxxxxx",
  "commissioningDate": "2026-05-21",
  "uptime": "3 days 02:14:00",
  "cpmsConnectionStatus": "connected",
  "rebootLogs": []
}
```

### 4.2 État temps réel de la borne

Tester la réception de `StatusNotification` avec les états:

- Available
- Occupied
- Charging
- Reserved
- Unavailable
- Faulted
- Preparing
- Finishing
- SuspendedEV
- SuspendedEVSE

#### Comment tester

Envoyer plusieurs `StatusNotification` et vérifier dans `/api/cp`:

- `status_by_cp`
- l'historique des événements dans `events`
- les logs dans `cpms_data/borne_logs/<borne>.json`

#### Exemple attendu

```json
{
  "status_by_cp": {
    "CP001": {
      "status": "Charging",
      "connector_id": 1,
      "error_code": "NoError"
    }
  }
}
```

### 4.3 Informations des connecteurs

Tester et renseigner:

- nombre de connecteurs
- type de connecteur
- puissance max
- courant max
- tension nominale
- phases

#### Important

Ces infos ne sont pas toujours envoyées automatiquement par la borne. Elles peuvent être ajoutées via:

```bash
POST /api/cp/CP001/meta
```

#### Exemple

```json
{
  "connectors": [
    {
      "connectorId": 1,
      "type": "Type2",
      "acdc": "AC",
      "maxPowerKw": 22,
      "maxCurrentA": 32,
      "nominalVoltageV": 230,
      "phases": 3
    },
    {
      "connectorId": 2,
      "type": "CCS2",
      "acdc": "DC",
      "maxPowerKw": 60,
      "maxCurrentA": 125,
      "nominalVoltageV": 400,
      "phases": 3
    }
  ]
}
```

### 4.4 Données de session de recharge

Tester:

- ID session
- heure de début
- heure de fin
- durée
- énergie consommée
- puissance instantanée
- courant instantané
- tension instantanée
- courbe de charge complète
- consommation minute par minute
- état véhicule pendant la charge
- cause d'arrêt
- temps d'inactivité après charge

#### Comment tester

Envoyer:

- `StartTransaction`
- `MeterValues`
- `StopTransaction`

Puis vérifier dans `/api/cp`:

- `sessions`
- `sessions[CP001][0].charge_curve`
- `sessions[CP001][0].kpis`
- `energy`

#### Résultat attendu

- session créée au `StartTransaction`
- session fermée au `StopTransaction`
- courbe par minute générée à partir des `MeterValues`
- KPI calculés:
  - `total_kwh`
  - `peak_kw`
  - `avg_kw`

### 4.5 Données utilisateur / authentification

Tester:

- RFID Tag ID
- méthode d'authentification
- autorisé / refusé
- historique des sessions utilisateur
- compte fleet / entreprise
- token OCPI
- Plug & Charge

#### Comment tester

Envoyer `Authorize` avec un `idTag` et vérifier:

- l'événement enregistré dans `events`
- l'historique dans `borne_logs`
- la session associée à l'utilisateur si `StartTransaction` suit

#### Exemple

```json
{
  "id_tag": "ABC123456",
  "result": "accepted"
}
```

### 4.6 Données énergétiques avancées

Tester:

- kWh total délivrés
- kWh par station
- kWh par utilisateur
- kWh par flotte
- pic de puissance
- load balancing
- répartition phase L1/L2/L3
- qualité électrique
- facteur de puissance
- consommation standby

#### Comment tester

Envoyer plusieurs `MeterValues` sur la session puis vérifier:

- `energy.total_kwh`
- `energy.kwh_per_cp`
- `sessions[...].kpis`
- `sessions[...].charge_curve`

### 4.7 Données financières

Tester si l'intégration paiement existe:

- prix session
- tarif par kWh
- tarif par minute
- frais parking
- TVA
- revenus opérateur
- revenus EVMapyTN
- part STEG
- historique transactions
- paiement réussi / échoué

#### Remarque

Le projet actuel ne calcule pas encore les paiements. Ces champs doivent être ajoutés dans `metadata` ou dans une couche de tarification séparée.

### 4.8 Alertes et maintenance

Tester:

- surchauffe
- défaut isolation
- défaut terre
- coupure réseau
- erreurs connecteur
- porte ouverte
- défaut lecteur RFID
- défaut compteur MID
- erreur communication
- overcurrent
- undervoltage
- logs d'erreurs
- diagnostic distant

#### Comment tester

Envoyer:

- `DiagnosticsStatusNotification`
- `FirmwareStatusNotification`
- `DataTransfer`
- `StatusNotification` avec `Faulted`

### 4.9 Commandes distantes via OCPP

Tester:

- StartTransaction
- StopTransaction
- Remote reboot
- Unlock connector
- Change configuration
- Firmware update
- Reset borne
- Smart charging profiles
- Limitation puissance
- Reservation borne

#### État actuel du projet

- `force_start` et `force_stop` existent côté serveur.
- Le démarrage à distance réel vers la borne peut être ajouté ensuite via un registre d'instances OCPP actives.

### 4.10 Smart Charging / Energy Management

Tester:

- dynamic load balancing
- limitation puissance selon STEG
- peak shaving
- répartition intelligente énergie
- gestion multi-bornes
- priorité flotte
- tarification dynamique
- smart schedules

#### État actuel

Non implémenté automatiquement, mais les données nécessaires peuvent venir des `MeterValues` et des métadonnées.

### 4.11 Données ESG / CO2

Tester:

- CO2 évité
- litres carburant économisés
- émissions par flotte
- score ESG
- impact environnemental
- KPI mobilité durable
- reporting entreprise

#### État actuel

À calculer à partir de l'énergie totale (`kWh`) et d'un facteur CO2 externe.

### 4.12 Données géographiques

Tester:

- latitude
- longitude
- ville
- zone
- site
- disponibilité géographique
- heatmaps d'utilisation
- zones sous-équipées

#### Comment tester

Renseigner dans `POST /api/cp/<cp_id>/meta`:

```json
{
  "latitude": 36.8065,
  "longitude": 10.1815,
  "city": "Tunis",
  "zone": "Centre-ville",
  "site": "Parking-01"
}
```

### 4.13 Analytics avancés

Tester:

- prévision occupation bornes
- prévision panne
- détection fraude
- optimisation déploiement
- analyse comportement utilisateur
- analyse rentabilité station
- prévision consommation réseau
- maintenance prédictive

#### État actuel

Non implémenté dans le code, mais les données brutes sont collectées pour un futur module IA.

### 4.14 Données spécifiques OCPP 2.0.1

Tester:

- device management avancé
- sécurité améliorée
- certificats
- monitoring composants
- transactions plus détaillées
- ISO 15118 natif
- Plug & Charge
- télémetrie avancée
- energy management

#### État actuel

Le code est basé sur OCPP 1.6. Pour OCPP 2.0.1, il faudra adapter la bibliothèque et les handlers.

## 5. Tests manuels recommandés

### Test 1 - Connexion et heartbeat

1. Brancher la borne.
2. Vérifier que le log montre `Heartbeat`.
3. Vérifier `GET /api/cp`.

Résultat attendu:

- borne visible
- `last_seen` mis à jour
- événement `Heartbeat` enregistré

### Test 2 - BootNotification

1. Redémarrer la borne.
2. Vérifier que `BootNotification` est loggé.
3. Vérifier `metadata` si la borne fournit modèle/fabricant/firmware.

### Test 3 - Session de recharge complète

1. Envoyer `Authorize`.
2. Envoyer `StartTransaction`.
3. Envoyer plusieurs `MeterValues`.
4. Envoyer `StopTransaction`.
5. Vérifier `sessions` et `energy` dans `/api/cp`.

### Test 4 - Statut temps réel

Envoyer successivement:

- `Preparing`
- `Charging`
- `SuspendedEV`
- `Finishing`
- `Available`

Puis vérifier `status_by_cp`.

### Test 5 - Erreur / maintenance

Envoyer `StatusNotification` avec `Faulted` ou un `errorCode` non nul.

Vérifier que l'erreur apparaît dans `events` et dans `borne_logs`.

### Test 6 - Métadonnées manuelles

Envoyer un JSON complet dans `POST /api/cp/CP001/meta` pour remplir:

- fabricant
- modèle
- numéro de série
- firmware
- ICCID / IMSI
- géolocalisation
- connecteurs
- puissance
- tarifs

### Test 7 - Force start / stop sans id_tag

Démarrer sans authentification:

```bash
curl -X POST http://127.0.0.1:5000/api/cp/CP001/force_start -d "connector_id=1&meter_start=0"
```

Arrêter ensuite:

```bash
curl -X POST http://127.0.0.1:5000/api/cp/CP001/force_stop -d "meter_stop=1000"
```

## 6. Ce qu’il faut vérifier dans `/api/cp`

Pour dire que la collecte est bonne, vérifier que `/api/cp` contient:

- `snapshot`
- `events`
- `borne_logs`
- `metadata`
- `sessions`
- `energy`
- `files`

Et pour une borne donnée:

```text
/api/cp?cp_id=CP001
```

## 7. Exemple de checklist de validation

- [ ] La borne se connecte
- [ ] Le heartbeat est reçu
- [ ] `BootNotification` est enregistré
- [ ] Les métadonnées sont visibles
- [ ] L’état temps réel est à jour
- [ ] Les `MeterValues` sont enregistrées
- [ ] Une session est créée
- [ ] Une session est fermée
- [ ] La courbe minute par minute est visible
- [ ] Les KPI énergie sont calculés
- [ ] Les alertes / erreurs sont visibles
- [ ] Les données géographiques sont renseignées
- [ ] Les données financières sont prêtes si tu ajoutes une tarification

## 8. Conclusion

Le projet collecte déjà une grande partie des informations de base et des données de session.

Pour obtenir le maximum de données dans `/api/cp`, il faut surtout:

- envoyer `BootNotification`, `StatusNotification`, `MeterValues`, `StartTransaction`, `StopTransaction`
- compléter les métadonnées manuelles via `/api/cp/<cp_id>/meta`
- enrichir ensuite les calculs énergie, ESG et finance selon tes besoins

Si tu veux, je peux maintenant te faire un fichier de tests `pytest` qui valide automatiquement:

- `/api/cp`
- `/api/cp?cp_id=...`
- `meta`
- `force_start`
- `force_stop`
- `sessions`
- `energy`
