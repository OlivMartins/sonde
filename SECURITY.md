# Politique de sécurité

## Versions maintenues

| Version | Correctifs de sécurité |
|---|---|
| 0.1.x | Oui |

## Signaler une vulnérabilité

Merci de ne pas ouvrir de ticket public pour une faille de sécurité.

Utilisez le signalement privé de GitHub : onglet **Security** du dépôt, puis **Report a vulnerability**. Le rapport n’est visible que des mainteneurs.

Pour un traitement rapide, précisez :

- la version de Sonde et le composant concerné (agent, faux LLM, collector, schéma ClickHouse, tableau de bord) ;
- la configuration utilisée, sans secret réel ;
- les étapes pour reproduire et l’impact constaté.

Le correctif est publié dans une nouvelle version, accompagné d’un avis de sécurité. Le rapporteur y est crédité s’il le souhaite.

## Périmètre

Sont concernés le code de l’agent et du faux LLM, la configuration du collector (notamment le masquage des données sensibles), le schéma ClickHouse, le provisionnement Grafana et le fichier Docker Compose fournis dans ce dépôt.

Sonde est conçu pour un poste de travail : identifiants par défaut, accès anonyme à Grafana et ports limités à `127.0.0.1`. Avant tout usage partagé, changez les identifiants dans `.env` et suivez les recommandations de [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#passer-en-production). Les règles de masquage réduisent le risque de stocker des données sensibles, sans le supprimer : relisez-les pour vos propres formats de journaux.
