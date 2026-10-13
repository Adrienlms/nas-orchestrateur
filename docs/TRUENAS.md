# Installer Super Agent sur TrueNAS SCALE

Testé pour TrueNAS SCALE 24.10 (Electric Eel) ou plus récent, qui fait tourner
les applications avec Docker.

## 1. Préparer un dossier pour les données

Dans **Datasets**, crée un dataset, par exemple `tank/apps/super-agent`
(remplace `tank` par le nom de ton pool). Dans ses permissions, donne-le à
l'utilisateur et au groupe **apps** (identifiant 568).

Ce dossier contiendra la configuration, l'historique des tâches et les projets
générés. Il survit aux mises à jour de l'application.

## 2. Récupérer tes clés

Mets au moins une des valeurs suivantes, selon ce que tu as :

- **Abonnement Claude Pro ou Max** : sur n'importe quel ordinateur où Claude
  Code est installé, lance `claude setup-token`, connecte-toi, et copie le
  jeton affiché. C'est la valeur de `CLAUDE_CODE_OAUTH_TOKEN`.
- **Gratuits** : une clé Google AI Studio (`GEMINI_API_KEY`), Mistral
  (`MISTRAL_API_KEY`), Groq (`GROQ_API_KEY`) ou OpenRouter
  (`OPENROUTER_API_KEY`).
- **Payants**, seulement si tu le veux : `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`.

- **Mode « dépôt GitHub »**, pour que l'équipe modifie un dépôt existant (y
  compris celui de Super Agent) : crée un jeton sur GitHub, **Settings** >
  **Developer settings** > **Fine-grained tokens** > **Generate new token**.
  Limite-le aux dépôts concernés et donne-lui seulement **Contents** et
  **Pull requests** en lecture et écriture. C'est la valeur de `GITHUB_TOKEN`.

## 3. Installer l'application

1. **Apps** > **Discover Apps** > menu ⋮ > **Install via YAML**.
2. Nom : `super-agent`.
3. Colle le contenu de [`docker-compose.yml`](../docker-compose.yml), puis
   modifie :
   - `SUPER_AGENT_PASSWORD` : le mot de passe de l'interface ;
   - les clés de l'étape 2 (laisse `""` pour celles que tu n'as pas) ;
   - le chemin du volume : `/mnt/TON-POOL/apps/super-agent:/data` ;
   - le port à gauche (`8090`) s'il est déjà pris sur ton NAS.
4. **Save**. TrueNAS télécharge l'image et démarre le conteneur.

Ouvre ensuite `http://IP-DU-NAS:8090`. Le navigateur demande un identifiant :
mets ce que tu veux comme nom, et ton mot de passe.

L'onglet **Fournisseurs** montre qui est prêt. Une tâche qui échoue avec
« Claude Code n'est pas connecté » veut dire que le jeton est absent ou
expiré.

## Mettre à jour

À chaque modification poussée sur la branche `main` du dépôt, GitHub
reconstruit l'image `ghcr.io/adrienlms/nas-orchestrateur:latest`. Dans
TrueNAS, ouvre l'application puis **Update** (ou arrête-la et redémarre-la)
pour récupérer la nouvelle version.

## Si l'image ne se télécharge pas

Le premier envoi d'une image sur GitHub la crée en **privé**. Deux solutions :

- la rendre publique : sur GitHub, ton profil > **Packages** >
  `nas-orchestrateur` > **Package settings** > **Change visibility** > Public
  (l'image ne contient aucune clé, elles restent sur ton NAS) ;
- ou la garder privée et donner à TrueNAS un accès : **Apps** > **Configuration**
  > **Docker Registries**, registre `ghcr.io`, ton identifiant GitHub et un
  jeton GitHub avec le droit `read:packages`.

## Sans passer par GitHub

Depuis le shell de TrueNAS (**System** > **Shell**), dans une copie du dépôt :

```bash
docker build -t super-agent:local /mnt/TON-POOL/chemin/vers/nas-orchestrateur
```

puis, dans le YAML, remplace l'image par `super-agent:local` et ajoute
`pull_policy: never`.

## Sécurité

- Les tests générés par les IA s'exécutent **dans le conteneur**, pas
  directement sur ton NAS, et le conteneur n'a accès qu'au dossier de données.
- Garde un mot de passe. N'expose pas le port sur Internet ; pour un accès à
  distance, passe par un VPN (WireGuard, Tailscale…).
