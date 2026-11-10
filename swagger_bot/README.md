# Bot de benchmark Swagger

Ce bot évalue plusieurs modèles IA via l'API Swagger. Il appelle `/health`,
envoie chaque question à `/ask`, mesure la latence et compare chaque réponse
avec la réponse de référence correspondante dans `answers.json`.

Le résultat est enregistré dans un rapport PDF unique contenant les résultats
séparés par modèle, les réponses de l'IA, les scores de comparaison et une
conclusion avec des métriques comparatives.

## Utilisation

## Prérequis et lancement

L'API doit être ouverte et accessible avant de lancer le bot. Dans un premier
terminal, démarre l'API depuis la racine du projet :

```powershell
python api.py
```

L'API doit rester active sur `http://127.0.0.1:8000`. Dans un deuxième
terminal, lance ensuite le bot avec `bot.py` :

```powershell
python swagger_bot\bot.py --timeout 180
```

Le rapport PDF est généré uniquement après l'exécution du bot. Par défaut, il
est créé à la racine du projet sous le nom `benchmark_report.pdf`. L'API doit
donc rester ouverte pendant toute la durée du benchmark.

Ou directement depuis le dossier `swagger_bot` :

Pendant le benchmark, chaque modèle reçoit toutes les questions. Le bot affiche
la progression, la réponse reçue et le pourcentage de comparaison calculé.
Si une IA dépasse le timeout, la latence maximale configurée est enregistrée,
la note est automatiquement fixée à `0 %`, puis le bot passe à la question
suivante sans interrompre le benchmark. Cela fonctionne aussi lorsque l'API
retourne une erreur HTTP contenant un timeout : l'erreur est enregistrée comme
un résultat à `0 %` au lieu d'arrêter la série.

Options :

```powershell
python swagger_bot\bot.py --url http://127.0.0.1:8000 --timeout 60
python swagger_bot\bot.py --questions swagger_bot\questions.json
python swagger_bot\bot.py --output rapports\benchmark.pdf
python swagger_bot\bot.py --models qwen2.5:1.5b llama3.2:3b
python swagger_bot\bot.py --answers answers.json
```

Le rapport est créé par défaut dans `benchmark_report.pdf`.

Le tableau de modèles par défaut contient :

```python
(
    "qwen2.5:1.5b",
    "llama3.2:3b",
    "bonsai-2:2b",
    "phi3:3.8b",
    "nuextract:3.8b",
)
```

Avec `--models`, indique chaque modèle séparément :

```powershell
python swagger_bot\bot.py --models qwen2.5:1.5b llama3.2:3b --timeout 180
```

Chaque modèle reçoit toutes les questions à tour de rôle. Les résultats sont
regroupés par modèle dans le même PDF.

## Réponses de référence

`answers.json` contient les réponses attendues, indexées par le numéro de
question (`"1"`, `"2"`, etc.) :

```json
{
  "1": "Réponse attendue à la question 1.",
  "2": "Réponse attendue à la question 2."
}
```

Le score combine le recouvrement des mots et la similarité textuelle. Une
réponse ayant dépassé le timeout reçoit automatiquement `0 %`.

## Rapport PDF

Le PDF contient :

- les questions et les réponses de chaque modèle ;
- la latence de chaque appel ;
- le pourcentage de comparaison avec `answers.json` ;
- la latence moyenne par modèle ;
- le score moyen par modèle ;
- le modèle le plus rapide ;
- le modèle ayant le meilleur score.

Le rapport précise que le benchmark analyse les capacités de perception et de
compréhension d'un transcript afin d'en faciliter la simplification.

## Timeout

Le timeout s'applique à chaque appel HTTP. Si une réponse dépasse cette durée,
le bot enregistre la durée maximale, attribue `0 %` et passe à la question
suivante sans interrompre le benchmark.

Le fichier de questions doit être un tableau JSON de textes, par exemple :

```json
[
  "Comment s'appelle le personnage principal ?",
  "Comment le problème est-il résolu ?"
]
```