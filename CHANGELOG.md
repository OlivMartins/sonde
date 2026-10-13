# Journal des modifications

Toutes les évolutions notables de Sonde sont consignées ici. Le format s’inspire de [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/) et les versions suivent le [versionnage sémantique](https://semver.org/lang/fr/).

## [0.3.1] - 2026-10-13

### Corrigé

- Une latence pouvait s’afficher en siècles. Les horodatages d’un span viennent de l’horloge murale du processus ; si elle recule pendant le span (synchronisation NTP, sortie de veille, Docker Desktop qui recale sa machine virtuelle), le span finit avant d’avoir commencé, et sa durée négative devient environ 584 ans dans la colonne `UInt64`. Les vues la comptent désormais pour 0, et l’explorateur de traces aussi.

### Ajouté

- Contrôle « Durées négatives » dans la rangée « Qualité des données », alimenté par la colonne `NegativeDurations` de `span_quality_1m`.
- Capture du tableau de bord dans le README.

### À savoir avant de mettre à jour

- Les vues et `span_quality_1m` changent : repartez de zéro avec `docker compose down -v`, puis `docker compose up -d --build`.

## [0.3.0] - 2026-10-13

Juger la qualité des réponses, échantillonner les traces sans fausser les chiffres, et s’ouvrir aux autres outils. Une tâche « réussie » n’est pas forcément une bonne réponse, garder chaque span coûte cher, et Sonde doit pouvoir alimenter Langfuse ou Phoenix plutôt que les concurrencer.

### À savoir avant de mettre à jour

- **Schéma ClickHouse.** Les vues matérialisées lisent désormais `otel_spans_stream`, une table au moteur `Null` qui reçoit 100 % des spans, et non plus `otel_traces`. Nouvelles tables : `otel_logs`, `genai_evaluations`, `span_quality_1m`, et la vue `genai_run_scores`. Repartez de zéro avec `docker compose down -v`, puis `docker compose up -d --build`. Si vous écrivez des spans dans ClickHouse par un autre chemin que le collector de Sonde, écrivez-les aussi dans `otel_spans_stream`, sans quoi les vues ne voient plus rien.
- **Collector.** Le pipeline `traces` devient `traces/in`, qui normalise et filtre, puis se divise en `traces/aggregates` (sans le contenu, vers le flux) et `traces/raw` (vers `otel_traces`). Un pipeline `logs` reçoit les évaluations. Reportez vos modifications locales sur cette structure.
- **Nouveau service.** `evaluator` démarre avec la stack et note les réponses avec le juge simulé. Avec un vrai modèle, renseignez `JUDGE_MODEL`, `JUDGE_BASE_URL` et `JUDGE_API_KEY` : chaque note coûte un appel.
- **Tableau de bord.** Une rangée « Qualité des réponses » s’insère après « Coût et test A/B », et la table du test A/B passe en pleine largeur.

### Ajouté

- Évaluateur asynchrone (`python -m sonde_agent.evaluator`) : il lit les exécutions retenues pour l’évaluation, demande une note de 1 à 5 à un LLM juge et émet l’événement standard `gen_ai.evaluation.result`, rattaché au span évalué. Il lit par pages, ne juge jamais deux fois et abandonne une évaluation en échec. Ses propres appels sont tracés et chiffrés sous l’agent `sonde-evaluator`.
- Échantillonnage pour l’évaluation (`SONDE_EVAL_SAMPLE_RATE`, 30 % dans la démo) : une exécution aboutie garde sa question et sa réponse finale, rien d’autre.
- Coût par bonne réponse dans le test A/B, avec le nombre de réponses jugées, la note moyenne et le taux de bonnes réponses.
- Rangée « Qualité des réponses » : note du juge par version, réponses jugées, coût du juge, réponses les moins bien notées avec leur trace.
- Profil d’échantillonnage en queue (`docker-compose.sampling.yml`, `make up-sampling`) : un collector gateway garde les traces en erreur, avec outil refusé, nouvelle tentative, coût élevé ou retenues pour l’évaluation, plus 10 % du reste (`SONDE_BASELINE_SAMPLING_PERCENT`). Le routage par identifiant de trace garde chaque trace entière.
- Compteurs de qualité calculés sur le flux (`span_quality_1m`) : spans sans agent, attributs renommés, supprimés et masqués restent exacts sous échantillonnage.
- Colonne `trace_kept` dans les exécutions les plus chères : la trace brute existe-t-elle encore ?
- Surcouches d’export vers Langfuse et Arize Phoenix (`otel-collector/overlays/`), après normalisation et masquage.
- Faux LLM : juge `sim-judge`, et réponses bâclées du petit modèle (`small_model_shallow_rate`) pour donner du travail au juge.
- Documentation : évaluation, échantillonnage en queue, interopérabilité, limites du juge et de l’échantillonnage, piste de recherche vectorielle sur les échecs, nouvelles recettes SQL. Le README raconte d’où vient le projet.
- Tests : évaluateur (notes, événement, pagination), échantillonnage rejoué dans chDB et comparé à la vérité calculée sur tous les spans, surcouches du collector. En CI, toutes les configurations du collector sont validées, les évaluations doivent arriver, et une seconde stack tourne avec le gateway d’échantillonnage.
- `make evals` pour suivre le juge.

### Modifié

- La branche des agrégats retire le contenu des messages : seuls les spans bruts le portent.
- L’explorateur de traces s’ouvre sur la dernière exécution dont la trace brute a été gardée.
- `make down` et `make clean` arrêtent aussi le gateway d’échantillonnage.

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

[0.3.1]: https://github.com/OlivMartins/sonde/releases/tag/v0.3.1
[0.3.0]: https://github.com/OlivMartins/sonde/releases/tag/v0.3.0
[0.2.0]: https://github.com/OlivMartins/sonde/releases/tag/v0.2.0
[0.1.0]: https://github.com/OlivMartins/sonde/releases/tag/v0.1.0
