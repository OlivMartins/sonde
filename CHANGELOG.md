# Journal des modifications

Toutes les évolutions notables de Sonde sont consignées ici. Le format s’inspire de [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/) et les versions suivent le [versionnage sémantique](https://semver.org/lang/fr/).

## [0.1.0] - 2026-10-13

Première version : un socle d’observabilité pour agents IA, de la trace OpenTelemetry au tableau de bord Grafana, démarrable en une commande.

### Ajouté

- Stack Docker Compose : OpenTelemetry Collector 0.162, ClickHouse 26.8 LTS, Grafana 13.2 avec le plugin ClickHouse 4.22. Tous les ports écoutent sur `127.0.0.1` seulement.
- Agent de démonstration : un copilote SRE à outils, instrumenté selon les conventions GenAI d’OpenTelemetry (`invoke_agent`, `chat`, `execute_tool`), avec un générateur de charge qui teste deux versions en A/B.
- Faux LLM compatible OpenAI, sans dépendance : latence par modèle, tokens, cache de prompt, limites de débit, erreurs 5xx, boucles et outils hallucinés, réglables à chaud (`make chaos`, `make calm`).
- Masquage dans le collector : e-mails, secrets `clé=valeur` et jetons bearer sont remplacés avant stockage, et chaque span compte les valeurs masquées.
- Liste d’outils autorisés : un appel hors liste est refusé, renvoyé au modèle comme erreur et tracé avec `error.type=tool_not_allowed`.
- Modèle de données ClickHouse versionné : spans bruts compatibles avec l’exportateur, vues matérialisées vers des tables typées (exécutions, appels LLM, appels d’outils), agrégats à la minute, rétention étagée sur 3 jours, 30 jours et 400 jours.
- Coût calculé à la lecture : prix par modèle versionnés dans le temps, appliqués par `ASOF JOIN`, y compris le tarif réduit des tokens servis par le cache.
- Tableau de bord Grafana généré par du code : coût, test A/B, croissance du contexte, issues, latence, limites de débit, outils, masquage, exécutions les plus chères et vue de trace.
- Recettes SQL prêtes à l’emploi (`docs/sql-cookbook.md`) et description de l’architecture (`docs/ARCHITECTURE.md`).
- Tests : arbre de spans de l’agent ; rejeu des traces dans ClickHouse embarqué (chDB) pour exécuter chaque vue, chaque panneau et chaque requête documentée.
- Intégration continue : tests à chaque push, puis démarrage de la stack complète et interrogation de chaque panneau à travers Grafana. À la publication d’une release, l’archive, sa somme de contrôle et le tableau de bord sont construits et joints automatiquement.
- Politique de sécurité (`SECURITY.md`), licence Apache 2.0 et `NOTICE`.

[0.1.0]: https://github.com/OlivMartins/sonde/releases/tag/v0.1.0
