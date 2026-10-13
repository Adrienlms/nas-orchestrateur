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
