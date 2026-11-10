# sonar_fixer

Corrige des issues SonarQube **une règle à la fois, une issue à la fois**, avec le LLM interne
(Mistral / Devstral). Aucun agent : un script qui fait toujours la même chose.

Pour chaque issue : Mistral propose un remplacement de lignes → contrôles → écriture →
compilation → **gardé si ça compile, fichier restauré sinon**.

**Aucune opération git.** Les fichiers sont modifiés directement dans ton repo ; tu relis,
commits et pousses toi-même. Les originaux sont sauvegardés et `undo` annule un run.

## Prérequis
- Python 3.8+ (rien d'autre à installer : aucune dépendance)
- Le repo local sur **la même branche que celle analysée par Sonar**

## Installation
1. Copier `.env.example` en `.env` dans le même dossier que le script, puis le remplir
   (UTF-8 ou ANSI/Bloc-notes, les deux marchent).
2. `python sonar_fixer.py check` : teste Sonar, Mistral, les encodages des fichiers et la compilation.
   Tant qu'il y a un `ÉCHEC`, corriger le `.env` (le message dit quoi faire).

## Utilisation
```
python sonar_fixer.py stats                                    # top des règles
python sonar_fixer.py fix --rule java:S1155 --max 20 --dry-run # simulation : rien n'est modifié
python sonar_fixer.py fix --rule java:S1155 --max 20           # modifie les fichiers
python sonar_fixer.py undo                                     # annule le dernier run
```
Pendant `fix`, une barre de progression indique l'issue en cours, l'étape (Mistral, contrôles,
compilation), le chrono et le temps restant estimé. `Ctrl+C` arrête proprement : le fichier en cours
est restauré, et relancer la même commande reprend là où ça s'est arrêté.

## Après un run
Tout est dans `reports/<règle>_<date>/` :
- `modifications.diff` : toutes les modifications à relire (ou `git diff` dans ton repo)
- `rapport.csv` : une ligne par issue (s'ouvre dans Excel, accents compris)
- `logs/` : pour chaque SKIP/REJECT, la réponse brute de Mistral et la sortie de compilation
- `backup/` : les fichiers d'origine (utilisés par `undo`)

`undo` ne touche pas à un fichier que tu as retouché à la main depuis le run (il te le signale).

## Statuts
- **OK** : corrigé et compile
- **SKIP** : non traité (Mistral refuse, issue sans ligne, code local différent de l'analyse Sonar…)
- **REJECT** : proposition écartée (illisible, hors zone, trop grosse, accents abîmés, ne compile pas)

## Accents et encodages
L'encodage de chaque fichier est détecté (UTF-8, UTF-8 avec BOM, ANSI/Windows-1252, ISO-8859-1) et
conservé à l'octet près, fins de ligne comprises. Une proposition qui perd, modifie ou ajoute un
accent ou un caractère spécial est rejetée. `check` affiche les encodages trouvés dans le repo.

## Sans compilation
`BUILD_CMD` vide dans `.env` : le script applique les corrections sans compiler. Tous les autres
contrôles restent actifs, mais une correction qui casse la compilation peut passer : compile
toi-même et relis `modifications.diff` (ou `git diff`) avant de commiter.

## Conseils
- Commencer par des règles simples et fréquentes, `--max 20`, relire, puis augmenter.
- Une règle avec beaucoup de REJECT : passer à la suivante (`--retry` pour retenter plus tard).
- Après merge, relancer une analyse Sonar avant de retraiter la même règle.
- Erreur de certificat : renseigner `CA_BUNDLE`. Proxy : ajouter les hôtes internes à `NO_PROXY`.
