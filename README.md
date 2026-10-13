# Super Agent

Une équipe d'agents IA qui développe à ta place : un **architecte** rédige la
spécification, un **développeur** écrit le code, un **testeur** écrit les tests,
puis un **relecteur** juge le résultat. Tant que ce n'est pas approuvé, le code
repart en correction (boucle critique → correction, 3 tours par défaut).

Chaque agent est servi par **le modèle le plus compétent disponible** pour sa
tâche, parmi plusieurs fournisseurs (Anthropic, OpenAI, Google, Mistral, Groq,
OpenRouter, Ollama en local). Quand un fournisseur n'a plus de crédit ou de
quota, le système **bascule automatiquement** sur le suivant et s'en souvient
pendant une heure.

Python 3.11 ou plus, aucune dépendance à installer.

## Démarrage

```bash
cd nas-orchestrateur
python -m super_agent demo        # démonstration hors ligne, sans clé
```

La démo simule un Claude à court de crédit : on voit la bascule vers un autre
fournisseur, un premier tour refusé par le relecteur (un test échoue), la
correction, puis l'approbation.

Pour de vrai, ajoute au moins une clé d'API puis :

```bash
export ANTHROPIC_API_KEY=...      # et/ou GEMINI_API_KEY, MISTRAL_API_KEY, GROQ_API_KEY…
python -m super_agent status      # qui est prêt, qui est en pause, quel modèle pour quelle tâche
python -m super_agent run "Un convertisseur CSV vers JSON en ligne de commande" --out sortie/csv
```

Le dossier de sortie contient le code, les tests, `SPEC.md` et `REPORT.md`
(qui a fait quoi, remarques de chaque tour, jetons consommés).

Options utiles : `--strategy economy` (le modèle le moins cher jugé assez bon
plutôt que le meilleur), `--rounds 5`, `--no-tests` (ne pas exécuter les tests
générés). `python -m super_agent reset` efface les pauses mémorisées.

## Interface web

```bash
python -m super_agent web --port 8080     # puis http://localhost:8080
```

Tu y lances des demandes, suis le journal en direct, lis le rapport, parcours
les fichiers produits et télécharges le projet en .zip. L'onglet
**Fournisseurs** montre quelles IA sont prêtes ou en pause, et l'onglet
**Configuration** permet de modifier `config.toml`. Définis
`SUPER_AGENT_PASSWORD` pour protéger l'accès.

## Travailler sur un dépôt existant

Dans l'interface, choisis **Dépôt GitHub**, colle l'adresse du dépôt et décris
le changement voulu. L'équipe clone le dépôt, lit son code, fait les
modifications, lance les tests du dépôt (commande automatique ou celle que tu
indiques) et boucle avec le relecteur, qui juge le diff. Le résultat arrive
dans une **pull request** sur une branche `super-agent/…` : rien n'est
fusionné sans toi. Si le relecteur n'a pas approuvé, la PR est ouverte en
brouillon.

Il faut un jeton `GITHUB_TOKEN` limité aux dépôts concernés (droits
**Contents** et **Pull requests**). Les tests lancés par l'équipe ne voient ni
ce jeton ni les clés d'API.

C'est aussi comme ça que Super Agent s'améliore lui-même : donne-lui l'adresse
de son propre dépôt, relis sa PR, fusionne-la, et l'image se reconstruit.

Limites : pour un gros dépôt, seuls les fichiers qui tiennent dans le budget
de contexte (environ 120 000 caractères, README et petits fichiers d'abord)
sont lus en entier ; les autres n'apparaissent que dans l'arborescence.

## Sur un NAS ou un serveur, avec Docker

```bash
docker compose up -d                      # interface sur http://IP:8090
```

Le guide pas à pas pour TrueNAS SCALE est dans [docs/TRUENAS.md](docs/TRUENAS.md).
L'image est construite automatiquement par GitHub à chaque modification de
`main` : `ghcr.io/adrienlms/nas-orchestrateur:latest`.

## Important : crédit gratuit et clés d'API

Une clé d'API Anthropic consomme toujours des crédits payants, séparés de
l'abonnement. Pour utiliser plutôt le quota de ton abonnement **Pro ou Max**
(la fenêtre qui se recharge toutes les 5 heures), Super Agent passe par
Claude Code :

1. installe Claude Code et connecte-le une fois : `claude`, puis `/login` avec
   ton compte d'abonnement ;
2. c'est tout : le fournisseur `claude_abonnement` est prioritaire dans
   `config.toml`. Quand le quota est atteint, il est mis en pause jusqu'à
   l'heure de recharge et les autres fournisseurs prennent le relais ;
3. pour ne jamais payer de crédits Anthropic, ne définis pas
   `ANTHROPIC_API_KEY` (Super Agent la masque de toute façon à Claude Code).

Le plan gratuit de claude.ai n'inclut pas Claude Code et n'est donc pas
utilisable ainsi. Cet usage via Claude Code est prévu pour toi seul, sur ta
machine : Anthropic n'autorise pas à revendre ou partager l'accès d'un
abonnement dans un produit tiers.

Fournisseurs avec un niveau gratuit (conditions et limites changent souvent,
vérifie sur leur site) :

| Fournisseur | Gratuit | Variable |
|---|---|---|
| Google Gemini (AI Studio) | oui, avec limites journalières | `GEMINI_API_KEY` |
| Mistral | offre « Experiment » gratuite, limitée | `MISTRAL_API_KEY` |
| Groq | oui, avec limites de débit | `GROQ_API_KEY` |
| OpenRouter | modèles suffixés `:free`, limités | `OPENROUTER_API_KEY` |
| Ollama | gratuit et illimité, tourne sur ton ordinateur | aucune (`enabled = true`) |
| Anthropic, OpenAI | payants à l'usage | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` |

Une combinaison sans frais : Gemini + Groq + Mistral + Ollama. Avec un peu de
crédit Anthropic, Claude passe en premier et les gratuits prennent le relais
quand il est épuisé.

## Régler le choix des modèles

Tout est dans `config.toml`. Chaque modèle a un coût (0 à 5) et une note de
0 à 10 par type de tâche (`architecture`, `code`, `test`, `review`). Le routeur
prend la meilleure note parmi les fournisseurs prêts. Ces notes sont des
estimations de départ : ajuste-les selon tes résultats. Les noms de modèles
sont des exemples, vérifie-les dans la documentation de chaque fournisseur.

Le relecteur est choisi de préférence chez un autre fournisseur que le
développeur, pour un regard vraiment indépendant.

## Comment la bascule décide

| Réponse du fournisseur | Action |
|---|---|
| crédit ou quota épuisé (402, « credit balance », `insufficient_quota`…) | pause 60 min, mémorisée entre lancements |
| limite de débit (429) | pause selon `retry-after` ou 60 s |
| panne ou surcharge (5xx, 529, réseau) | pause 20 s |
| clé refusée (401/403) | fournisseur ignoré pour la session |
| requête invalide (autre 4xx) | arrêt, car changer de fournisseur n'y changerait rien |

## Structure

```
super_agent/
  providers.py     connecteurs (abonnement via Claude Code, API Anthropic, format OpenAI pour les autres)
  errors.py        classement des erreurs (quota, débit, panne…)
  router.py        choix du modèle par tâche, bascule, pauses mémorisées
  agents.py        rôles et consignes des agents
  orchestrator.py  enchaînement et boucle critique → correction
  workspace.py     écriture des fichiers produits, exécution des tests
  repo.py          mode dépôt : clone, diff, branche, pull request
  web.py, static/  interface web
Dockerfile, docker-compose.yml, docs/TRUENAS.md   déploiement
config.toml        fournisseurs, modèles, notes
tests/             python -m unittest discover -s tests -t .
```

## Limites du prototype

- Les tests générés sont exécutés sur la machine qui fait tourner Super Agent
  (dans le conteneur avec Docker) : c'est du code écrit par une IA.
- Les agents travaillent en séquence, un fichier complet à la fois : adapté à
  de petits projets, pas encore à une grosse base de code existante.
- Pas encore d'outils pour les agents (lecture d'un dépôt, recherche web) ni de
  parallélisme, et une seule tâche s'exécute à la fois (les autres attendent).
