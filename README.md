<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/banner-dark.png">
    <img src="docs/assets/banner-light.png" width="100%" alt="Sonde : vos agents IA sous observation, de la trace OpenTelemetry à la requête ClickHouse">
  </picture>
</p>

<p align="center">
  <b>Un agent IA boucle, appelle des outils qu’on ne lui a pas donnés, laisse fuiter des données dans ses prompts et consomme des tokens en silence.<br>
  Sonde rend tout cela visible, avec des standards ouverts, en une commande.</b>
</p>

<p align="center">
  <a href="https://github.com/OlivMartins/sonde/releases/latest"><img src="https://img.shields.io/badge/version-0.3.1-F2C230?style=flat-square&labelColor=1B2A33" alt="Version 0.3.1"></a>
  <a href="https://github.com/OlivMartins/sonde/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/OlivMartins/sonde/ci.yml?branch=main&style=flat-square&label=tests&labelColor=1B2A33" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/licence-Apache%202.0-1E7A68?style=flat-square&labelColor=1B2A33" alt="Licence Apache 2.0"></a>
  <img src="https://img.shields.io/badge/OpenTelemetry-GenAI-53636B?style=flat-square&labelColor=1B2A33" alt="OpenTelemetry GenAI">
  <img src="https://img.shields.io/badge/ClickHouse-26.8%20LTS-53636B?style=flat-square&labelColor=1B2A33" alt="ClickHouse 26.8 LTS">
  <img src="https://img.shields.io/badge/Grafana-13-53636B?style=flat-square&labelColor=1B2A33" alt="Grafana 13">
</p>

<p align="center">
  <a href="#démarrer"><b>Démarrer</b></a>
  &nbsp;&nbsp;|&nbsp;&nbsp;
  <a href="#brancher-votre-propre-agent"><b>Brancher votre agent</b></a>
  &nbsp;&nbsp;|&nbsp;&nbsp;
  <a href="#sous-le-capot"><b>Sous le capot</b></a>
  &nbsp;&nbsp;|&nbsp;&nbsp;
  <a href="docs/ARCHITECTURE.md"><b>Architecture</b></a>
  &nbsp;&nbsp;|&nbsp;&nbsp;
  <a href="docs/sql-cookbook.md"><b>Requêtes SQL</b></a>
</p>

<br>

## En 30 secondes

- **Le problème.** Mettre un agent IA en production, c’est piloter à l’aveugle : on ne sait ni combien coûte une bonne réponse, ni pourquoi une exécution a bouclé, ni ce qui est parti dans les prompts.
- **La réponse.** Une stack d’observabilité complète, uniquement en briques ouvertes : **OpenTelemetry → Collector → ClickHouse → Grafana**.
- **Ce qu’on obtient.** Coût par bonne réponse, test A/B entre deux versions d’agent, note d’un LLM juge, alertes sur la qualité des données, masquage des données sensibles.
- **Ce qu’il faut.** Docker. Pas de clé d’API : un faux LLM simule le fournisseur, pannes comprises. Le tableau de bord se remplit en moins d’une minute.

<p align="center">
  <img src="docs/assets/dashboard.png" width="100%" alt="Le tableau de bord Sonde dans Grafana : exécutions, taux de succès, durée, dépense LLM, cache de prompt, outils refusés, puis le test A/B entre les deux versions de l’agent, les tokens et la croissance du contexte">
</p>

## Les trois questions auxquelles Sonde répond

<table>
<tr>
<td width="33%" valign="top">

**Combien ça coûte ?**

Coût par exécution, par tâche réussie et **par bonne réponse**, sous-agents compris, pour chaque version de l’agent. Les chiffres restent exacts même quand les traces brutes sont échantillonnées.

</td>
<td width="33%" valign="top">

**Est-ce que ça marche ?**

Taux de succès, boucles, latence, et la note d’un juge LLM qui tourne hors du chemin de l’agent. Et la justesse des chiffres eux-mêmes : appels sans tokens, spans orphelins, conversations fragmentées, avec alertes.

</td>
<td width="33%" valign="top">

**Est-ce que c’est sûr ?**

Le contenu n’est capturé qu’en cas d’échec. Le collector ne garde que les attributs autorisés et masque e-mails, clés et jetons. Les outils hors liste sont refusés et tracés.

</td>
</tr>
</table>

La démo embarque un **copilote SRE** qui enquête sur des incidents simulés, délègue l’analyse des journaux à un sous-agent et se teste en A/B contre une version plus légère de lui-même. Le faux LLM produit latence, tokens, cache de prompt, limites de débit, boucles, appels d’outils hallucinés et réponses bâclées : de quoi donner du travail à chaque panneau.

## Démarrer

```bash
git clone https://github.com/OlivMartins/sonde.git && cd sonde
docker compose up -d --build
```

Ouvrez <http://localhost:3000>. Le tableau de bord s’affiche directement.

Pour voir Sonde réagir à un incident, dégradez le fournisseur LLM à chaud :

```bash
make chaos   # limites de débit et erreurs 5xx côté LLM
make calm    # retour à la normale
```

<details>
<summary><b>Toutes les commandes</b></summary>

<br>

Le `Makefile` est facultatif : chaque raccourci correspond à une commande Docker.

| Effet | Commande | Raccourci |
| --- | --- | --- |
| Provoquer des limites de débit et des erreurs côté LLM | `curl -X POST localhost:8080/admin/faults -d '{"rate_limit_rate": 0.4}'` | `make chaos` |
| Revenir à la normale | `curl -X POST localhost:8080/admin/faults -d '{"rate_limit_rate": 0.04}'` | `make calm` |
| Suivre l’agent, une ligne par exécution | `docker compose logs -f agent` | `make logs` |
| Suivre le juge | `docker compose logs -f evaluator` | `make evals` |
| Échantillonner les traces brutes, chiffres intacts | `docker compose -f docker-compose.yml -f docker-compose.sampling.yml up -d --build` | `make up-sampling` |
| Ouvrir un shell ClickHouse | `docker compose exec clickhouse clickhouse-client -u sonde --password sonde -d otel` | `make sql` |
| Démarrer le socle seul, sans l’agent de démonstration | `docker compose up -d clickhouse otel-collector grafana` | `make infra` |
| Tout arrêter et supprimer les données | `docker compose -f docker-compose.yml -f docker-compose.sampling.yml down -v` | `make clean` |

</details>

## Brancher votre propre agent

Le socle accepte toute application qui émet de l’OTLP. Pointez votre SDK OpenTelemetry vers le collector :

```bash
make infra   # ClickHouse, collector et Grafana, sans l’agent de démo
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

Les spans qui suivent les [conventions GenAI d’OpenTelemetry](https://github.com/open-telemetry/semantic-conventions-genai) (`gen_ai.operation.name`, `gen_ai.usage.*`, `gen_ai.tool.name`…) arrivent automatiquement dans les tables typées et sur le tableau de bord. Les noms plus anciens (`gen_ai.system`, `gen_ai.usage.prompt_tokens`, spans de type OpenLLMetry…) sont convertis au passage.

Deux points à connaître :
- **Ventiler par agent** : posez `gen_ai.agent.name` et `gen_ai.agent.version` sur chaque span. [`telemetry.py`](agent/sonde_agent/telemetry.py) montre comment, et [`delegation.py`](agent/sonde_agent/delegation.py) comment garder le contexte quand un sous-agent part dans un thread.
- **Liste blanche** : le collector supprime les attributs qu’il ne connaît pas. Ajoutez les vôtres dans [`otel-collector/config.yaml`](otel-collector/config.yaml) ; le panneau « Attributs supprimés » montre ce qui est écarté.

Pour tracer un vrai modèle, n’importe quelle API compatible OpenAI convient. Exemple avec Ollama :

```bash
LLM_BASE_URL=http://host.docker.internal:11434/v1 LLM_PROVIDER=ollama \
AGENT_V1_MODEL=qwen3:8b AGENT_V2_MODEL=llama3.2:3b docker compose up -d agent
```

### Avec Langfuse ou Arize Phoenix

Sonde ne remplace pas ces plateformes, il peut les alimenter. Une surcouche de configuration du collector leur envoie une copie des traces **après** normalisation, liste blanche et masquage : vos règles de confidentialité s’appliquent avant que quoi que ce soit ne quitte la machine.

```bash
otelcol --config=config.yaml --config=overlays/langfuse.yaml   # ou overlays/phoenix.yaml
```

Les variables à renseigner et un exemple Docker Compose sont dans [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#interopérabilité).

## Sous le capot

Quelques choix qui font la différence entre une démo et un socle exploitable :

- **Des chiffres justes sous échantillonnage.** Le collector écrit chaque span dans une table ClickHouse au moteur `Null` : rien n’est stocké, mais les vues matérialisées voient passer 100 % du flux et en tirent les agrégats. On peut alors ne garder que 10 % des traces brutes (plus toutes les traces en erreur ou coûteuses, grâce à l’échantillonnage en queue) sans fausser un seul chiffre. Les tests le vérifient contre une vérité calculée sur tous les spans.
- **Un coût historisé.** Les prix des modèles sont versionnés dans le temps et rattachés à chaque appel par `ASOF JOIN` : changer un tarif ne réécrit pas le passé.
- **La confidentialité dans le pipeline, pas dans l’application.** Liste blanche stricte des attributs, masquage des secrets, capture du contenu limitée aux échecs. Les [limites connues](docs/ARCHITECTURE.md#limites-connues) du masquage par regex sont documentées, avec la place d’un NER comme Presidio.
- **Des conventions qui bougent.** Les conventions GenAI d’OpenTelemetry évoluent vite : le collector normalise les anciens noms et marque chaque span converti, et un panneau signale les attributs renommés ou supprimés.
- **L’évaluation hors du chemin critique.** Le LLM juge lit les exécutions après coup et émet l’événement standard `gen_ai.evaluation.result`. Ses propres appels sont tracés et chiffrés comme ceux de n’importe quel agent.
- **Les pièges du contexte de trace.** La délégation à un sous-agent dans un thread perd le contexte OpenTelemetry si l’on n’y prend garde. Sonde montre le correctif, injecte volontairement la panne et la détecte (spans orphelins, conversations fragmentées, alertes Grafana).

| Brique | Rôle |
| --- | --- |
| `agent/` | Agent Python à outils, instrumenté à la main selon les conventions GenAI, générateur de charge et évaluateur asynchrone |
| `fake-llm/` | Simulateur compatible OpenAI, juge compris, pannes réglables à chaud sur `/admin/faults` |
| `otel-collector/` | Réception OTLP, normalisation, liste blanche et masquage, écriture par lots ; gateway d’échantillonnage et surcouches d’export |
| `clickhouse/initdb/` | Tout le modèle de données : spans bruts, vues matérialisées, agrégats, prix, évaluations |
| `grafana/` | Source de données, tableau de bord et alertes, tous générés par du code |

Le détail est dans [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), et les requêtes prêtes à l’emploi dans le [SQL cookbook](docs/sql-cookbook.md).

## Tests

```bash
make dev && make lint && make test
```

- Les traces de l’agent sont vérifiées span par span, sous-agents et modes de capture compris.
- Des exécutions sont rejouées dans ClickHouse embarqué ([chDB](https://github.com/chdb-io/chdb)), exactement comme les écrit le collector : chaque vue, chaque panneau, chaque alerte et chaque requête de la documentation y est exécuté.
- La CI démarre la stack complète, y envoie un span au format ancien et vérifie qu’il ressort normalisé, filtré et masqué. Elle recommence avec l’échantillonnage en queue : les échecs doivent être gardés et les chiffres rester exacts.

## D’où vient Sonde

Après avoir utilisé LangSmith, j’ai voulu comprendre ce que font vraiment les plateformes d’observabilité IA, de Langfuse à Arize Phoenix en passant par SigNoz. Comment une exécution d’agent devient-elle une trace ? Où se calcule son coût ? Que deviennent les prompts ? Que perd-on en échantillonnant ? Comment note-t-on une réponse ? Le plus sûr moyen de le savoir était de refaire le chemin moi-même, pièce par pièce, avec des standards ouverts.

Sonde n’est donc pas un concurrent de ces outils, plutôt leur vue éclatée : chaque choix y est visible, testé et réversible.

## Auteur

Olivier, tech lead sur des plateformes de logs à grande échelle et des backends d’observabilité pour agents IA. OpenTelemetry, ClickHouse, Kafka, Elasticsearch.

Une question sur Sonde, un projet d’observabilité ou de migration ? Ouvrez une issue ou écrivez-moi sur [LinkedIn](https://www.linkedin.com/in/olivf).

Voir aussi [Passerelle](https://github.com/OlivMartins/passerelle) : traduire des requêtes Elasticsearch en SQL ClickHouse et des pipelines Logstash en Vector.

## Licence

Apache 2.0. Voir [LICENSE](LICENSE).
