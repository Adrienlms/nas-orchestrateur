# Super Agent

Super Agent réalise des demandes de développement à ta place, sur un nouveau
projet ou sur un dépôt GitHub existant, et propose le résultat dans une pull
request. Il a deux façons de travailler :

- **Agent Claude** (par défaut) : une seule session Claude Code, sur ton
  abonnement, travaille directement dans le dossier. Elle cherche et lit
  seulement les fichiers utiles, modifie les lignes nécessaires et garde son
  contexte d'un tour à l'autre. Super Agent lance ensuite les tests, fait
  relire le diff par une autre IA (gratuite si tu en as configuré une) et
  renvoie les remarques à la même session tant que ce n'est pas approuvé.
  C'est le mode le plus économe en quota et celui qui donne les meilleurs
  résultats.
- **Équipe multi-IA** : un **architecte** rédige la spécification, un
  **développeur** écrit le code, un **testeur** écrit les tests et un
  **relecteur** juge le résultat. Chaque rôle est servi par le modèle le plus
  compétent disponible parmi plusieurs fournisseurs (Anthropic, OpenAI,
  Google, Mistral, Groq, OpenRouter, Ollama en local). Utile quand l'abonnement
  Claude est épuisé : Super Agent y bascule tout seul.

Quand un fournisseur n'a plus de crédit ou de quota, il est mis en pause
jusqu'à sa recharge et le système bascule sur le suivant.

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

Options utiles : `--mode team` (l'équipe multi-IA au lieu de l'agent Claude),
`--strategy economy` (en mode équipe, le modèle le moins cher jugé assez bon
plutôt que le meilleur), `--rounds 3`, `--no-tests` (ne pas exécuter les tests
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

En mode agent, la session lit elle-même les fichiers dont elle a besoin, quelle
que soit la taille du dépôt. En mode équipe, seuls l'architecte et le
développeur reçoivent le contenu du dépôt, dans la limite d'environ 120 000
caractères (README et petits fichiers d'abord) ; le testeur et le relecteur ne
voient que la spécification et le diff.

## Consommation du quota Claude

Chaque tâche affiche ce qu'elle a consommé : nombre d'appels, jetons lus
(contexte relu depuis le cache compris) et un **équivalent API** en dollars.
Il n'est pas facturé avec l'abonnement, mais c'est ce qui pèse sur ta fenêtre
de 5 heures.

Réglages qui comptent, dans `config.toml` :

| Réglage | Défaut | Effet |
|---|---|---|
| `agent_model` | `sonnet` | `opus` épuise le quota Pro plusieurs fois plus vite |
| `max_review_rounds` | 2 | chaque tour de correction relance une session |
| `max_calls_per_task` | 12 | garde-fou : la tâche s'arrête au-delà |
| `agent_review` | `true` | relecture du diff ; mets `false` pour l'économiser |

Ordre de grandeur sur ce dépôt (ajout d'une commande avec un test) : en mode
agent, 2 appels et environ 85 000 jetons lus, approuvé ; en mode équipe, 6
appels, environ 120 000 jetons lus, quatre fois plus de jetons écrits, et
non approuvé. Sur un plan Pro, compte quelques tâches moyennes par fenêtre de
5 heures avec Sonnet, bien moins avec Opus.

Si tu as installé une version précédente, ton `config.toml` (dans le dossier
de données) garde les anciens réglages, où Opus servait pour tout. Dans
l'onglet **Configuration**, clique sur **Config par défaut** puis sur
**Enregistrer**.

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

### Les offres gratuites, vérifiées en octobre 2026

Une précision d'abord : ces sites affichent tous des formules payantes et
mettent souvent en avant un « plan ». Les offres ci-dessous sont gratuites,
mais limitées, et elles changent souvent.

| Fournisseur | Ce qui est gratuit | Ce qu'il faut | Variable |
|---|---|---|---|
| Google Gemini | `gemini-3.8-flash` et les modèles « Flash » ; **pas** les modèles Pro | un compte Google, une clé créée sur [aistudio.google.com](https://aistudio.google.com/apikey) sans carte ; tes requêtes servent à améliorer leurs produits | `GEMINI_API_KEY` |
| Groq | plan gratuit, par exemple `openai/gpt-oss-120b` (environ 1 000 requêtes par jour) | un compte sur [console.groq.com](https://console.groq.com) | `GROQ_API_KEY` |
| Mistral | offre « Experiment » | un numéro de téléphone vérifié, sans carte ; les requêtes peuvent servir à entraîner leurs modèles | `MISTRAL_API_KEY` |
| OpenRouter | les modèles dont le nom finit par `:free`, 50 requêtes par jour (1 000 après un achat unique de 10 crédits) | un compte ; la liste change souvent, choisis un modèle sur [openrouter.ai/models?max_price=0](https://openrouter.ai/models?max_price=0) et ajoute-le dans `config.toml` | `OPENROUTER_API_KEY` |
| Ollama | tout, sans limite | un ordinateur assez puissant pour faire tourner le modèle | aucune (`enabled = true`) |
| Anthropic, OpenAI | rien | crédits payants à l'usage | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` |

Ces modèles gratuits sont nettement moins bons que Claude pour coder. Ils
servent surtout de relecteur et de relais quand l'abonnement est épuisé.

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
  agent_mode.py    mode agent : session Claude Code avec outils, tests, relecture du diff
  orchestrator.py  mode équipe : enchaînement des rôles et boucle critique → correction
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
- En mode agent, la session lit et modifie les fichiers mais ne lance aucune
  commande : c'est Super Agent qui exécute les tests, dans un environnement
  sans jetons ni clés, puis lui renvoie les échecs.
- En mode équipe, les agents réécrivent des fichiers complets : adapté à de
  petits changements.
- Une seule tâche s'exécute à la fois (les autres attendent).
