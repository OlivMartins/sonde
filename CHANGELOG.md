# Journal des modifications

Toutes les évolutions notables de Sonde sont consignées ici. Le format s’inspire de [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/) et les versions suivent le [versionnage sémantique](https://semver.org/lang/fr/).

## [0.2.0] - 2026-10-13

Robustesse face aux trois limites relevées sur la 0.1 : un masquage par regex présenté comme suffisant, des vues ClickHouse qui écrivent 0 en silence si un attribut change de nom, et un contexte de trace qui ne suit pas les sous-agents lancés dans un thread.

### À savoir avant de mettre à jour

- **Schéma ClickHouse.** La table `genai_agent_runs` gagne la colonne `ParentAgent`. Les scripts d’initialisation ne s’exécutent que sur un volume vide : repartez de zéro avec `docker compose down -v`, puis `docker compose up -d --build`. Les spans bruts ne sont gardés que 3 jours, la perte est limitée.
- **Capture du contenu.** `CAPTURE_MESSAGE_CONTENT` est remplacé par `SONDE_CAPTURE_CONTENT`, qui prend `off`, `errors` ou `all`. La démo passe de `all` à `errors`. `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` reste compris et vaut `all`.
- **Liste blanche.** Le collector supprime désormais tout attribut absent de sa liste, y compris sur les ressources. Si vous y branchez votre propre agent, ajoutez vos attributs dans `otel-collector/config.yaml` ; le panneau « Attributs supprimés » montre ce qui est écarté.
- **Tableau de bord.** Il est en français, et les panneaux par exécution ne comptent plus que les exécutions principales : un sous-agent est compté dans l’exécution qui l’a délégué.

### Ajouté

- Normalisation dans le collector (`transform/genai_normalize`) : `gen_ai.system`, `gen_ai.usage.prompt_tokens`, `gen_ai.usage.completion_tokens`, `gen_ai.usage.cache_read_input_tokens` et les spans de type OpenLLMetry sont convertis vers les conventions actuelles. Chaque span converti porte `sonde.semconv.legacy`.
- Capture `errors` : le contenu n’est enregistré que pour un appel LLM en échec, un appel d’outil en échec ou refusé, et la transcription complète d’une exécution qui n’a pas abouti, posée une seule fois sur son span `invoke_agent`.
- Sous-agent `log-analyst` : le copilote lui délègue l’analyse des journaux dans un `ThreadPoolExecutor`, avec propagation du contexte par `contextvars.copy_context()`. Son coût est rattaché à l’exécution parente.
- Faute injectable `SONDE_FAULT_LOST_CONTEXT_RATE` : une part des délégations part sans contexte, comme dans le bug classique, pour donner du travail aux contrôles de qualité.
- Rangée « Qualité des données » : appels LLM sans tokens, spans GenAI sans agent, spans orphelins, conversations fragmentées, attributs renommés, attributs supprimés, et la liste des conversations à recoller avec leurs traces.
- Trois règles d’alerte Grafana provisionnées (appels sans tokens, spans orphelins, conversations fragmentées), générées par le même code que le tableau de bord.
- Section « Limites connues » dans `docs/ARCHITECTURE.md` : ce que les regex ne voient pas, la place d’un NER comme Presidio, les frontières où le contexte Python se perd.
- Recettes SQL : coût des sous-agents, conversations fragmentées, spans orphelins, attributs renommés ou supprimés.
- Tests : couverture de la liste blanche par les attributs réellement émis, trois modes de capture, sous-agent avec et sans contexte, compteurs de qualité vérifiés au span près sur un rejeu avec parents perdus. En CI, un span au format ancien traverse la stack et doit ressortir normalisé, filtré et masqué ; les règles d’alerte doivent s’évaluer sans erreur.

### Modifié

- Redaction en liste blanche (`allow_all_keys: false`) au lieu de tout laisser passer.
- Le faux LLM adapte son plan aux outils qu’on lui propose : un sous-agent n’appelle que ses propres outils.
- Les résultats des contrôles de bout en bout sont publiés en annotations GitHub, lisibles sans ouvrir les journaux.

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

[0.2.0]: https://github.com/OlivMartins/sonde/releases/tag/v0.2.0
[0.1.0]: https://github.com/OlivMartins/sonde/releases/tag/v0.1.0
