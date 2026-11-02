# PAD

[English](README.md) · [中文](README.zh.md) · **Français** · [日本語](README.ja.md) · [Русский](README.ru.md)

**Un cerveau, qui pilote du travail sur plusieurs machines. L'état est au milieu, pas dans une tête.**

Un processus, un fichier SQLite, deux portes.

<img src="docs/preview/hero.gif" width="880" alt="Quatre panneaux : démarrage rapide, état de la flotte, surface d'outils MCP, rapport de tâche">

<sub>Les quatre panneaux sont des images d'une exécution réelle, générées par `docs/make_preview.py` à partir de la sortie réelle — pas une maquette.</sub>

---

## PAD n'est pas un agent

À clarifier d'abord, sinon tout le reste se lit de travers.

```
ChatGPT / Claude  (cerveau)  ← sans état ; envoie des objectifs, lit les résultats
        ↘
          PAD                ← aucune intelligence. État, routage, preuves
        ↙
  nœuds × N  (un par machine)  ← fait le travail ; déclare ce qu'il sait faire
```

**PAD ne réfléchit pas et n'exécute rien.** Il fait trois choses :

- se souvenir de qui fait quoi et où il en est (état des tâches)
- router le travail vers les nœuds qui ont déclaré la capacité correspondante
- conserver chaque rapport et chaque preuve (le registre)

Le cerveau est un ChatGPT ou Claude externe — ou votre propre ligne de commande. Il est **neuf à chaque fois** et ne se souvient de rien de la veille.

C'est toute la raison d'être de PAD : **ce que le cerveau ne peut pas retenir, PAD le retient ; ce que le cerveau ne peut pas voir, PAD le route.**

## Pourquoi c'est important

Un agent ordinaire, c'est un modèle dans une session. Son plafond est ce que le contexte de cette session peut contenir.

- Nouvelle session → amnésie, tout réexpliquer
- Autre machine → aucune idée de ce qui y est installé ni de ce qu'elle sait faire
- Un outil de plus → le contexte se resserre, jusqu'à ne plus tenir

PAD sort l'état de la session. Donc :

- **Le cerveau peut piloter des machines qu'il n'a jamais vues.** Il n'apprend jamais un nom d'hôte ; il déclare qu'une chose doit être faite, et PAD route vers le nœud qui a la capacité. Remplacer une machine, en ajouter une, en perdre une — aucun changement côté cerveau.
- **Plusieurs nœuds travaillent en même temps sans diverger.** Même définition de tâche en entrée, même registre en sortie.
- **Vous voyez l'ensemble.** Un seul `get_digest` donne l'état de tous les nœuds, pas la vue d'une session.

![Les quatre couches de PAD](docs/preview/architecture.jpg)

## Un cerveau peut gouverner plusieurs types de travail

C'est ce que PAD sait vraiment faire.

L'approche habituelle donne à chaque type de travail son propre agent. Ils ne partagent aucun état, il faut donc relayer à la main — répéter la sortie, les contraintes et l'avancement de l'étape précédente pour la suivante. Plus d'agents, plus de relais.

PAD ne fonctionne pas comme ça. **La clé de routage est une capacité — pas un nom d'hôte, pas une identité d'agent.**

```
              hub : tâches / état / preuves / historique
                            │
        ┌───────────────────┼───────────────────┐
   capacité A          capacité B          capacité C
   nœud 1, nœud 2      nœud 3              nœud 4, nœud 5
   forte concurrence   ressource exclusive  arrière-plan long
```

Un cerveau, les neuf mêmes outils, pilotant **des types de travail totalement différents en même temps**. Il ne sait pas qui fait quoi, sur quelle machine, avec quel outil — il déclare ce qu'il veut et PAD route vers le nœud qui a déclaré cette capacité.

**Voilà ce qu'apporte vraiment le multi-machine : pas plus de concurrence, mais plus de types de travail qu'un seul cerveau peut gouverner, avec un état partagé.**

Ces types restent isolés : une panne sur une machine ne contamine pas l'état d'une autre, et une capacité qui expire ne tire pas le reste vers le bas.

## Un seul hub, deux usages

**Gouverné** et **autonome** sont deux configurations du même système.

| | Gouverné | Autonome |
|---|---|---|
| Qui donne les ordres | un humain nomme la capacité et l'objectif en ligne de commande | le cerveau décide combien de tâches et quelles capacités |
| Contrainte | chaque champ fixé par un humain | liste de capacités déclarée par un humain, le reste au cerveau |
| Pour quoi | résultats clairs, acceptation stricte | recherche, décomposition, travail multi-tours |
| Coût du basculement | une ligne de YAML | une ligne de YAML |

**Les deux écrivent la même table, passent le même portail d'acceptation et apparaissent dans la même vue.** Rien en aval ne branche. Passer de gouverné à autonome ne demande aucune migration et ne coupe pas la piste d'audit.

C'est ce que veut dire « l'humain garde la décision » : **non pas que l'agent ne puisse jamais agir seul, mais que la frontière de son autonomie soit déclarée par vous et révocable à tout moment.**

Mesuré sur un hub, deux mois en production :

| | Piloté par le cerveau | Ligne de commande humaine | Planificateur |
|---|---|---|---|
| tâches | 854 | 39 | 36 |
| couverture des preuves | 96% | 47% | 100% |
| durée médiane | 14,6 min | 0,7 min | 341 min |
| longueur médiane d'objectif | 301 car. | 131 car. | 611 car. |
| capacités touchées | 12 | 8 | 1 |

Le travail confié au cerveau est **spécifié** — objectifs longs, critères d'acceptation, réparti sur plusieurs capacités. Le travail envoyé à la main est **dirigé** — commandes courtes, moins d'une minute. Les deux sont lisibles sur le même registre.

## Ce que ça résout

| Problème | Réponse de PAD |
|---|---|
| Une nouvelle session perd tout ; le contexte ne tient pas | L'état vit dans SQLite, pas dans la session. Le cerveau le relit |
| Le cerveau ignore ce qu'une machine a ou sait faire | Une table de capacités. Le cerveau déclare le besoin ; PAD route |
| Ajouter une machine impose d'éditer prompts et code | Ajouter une machine = un YAML ; zéro changement côté cerveau |
| Plusieurs agents divergent | Une définition de tâche en entrée, un registre en sortie |
| « Fini » sans preuve | La complétion doit répondre à chaque critère, sinon elle est marquée sans réponse |

## Ce qui distingue les trois modes

Personal Agent et Work Agent désignent deux modes de travail typiques, pas une taxonomie stricte des produits.

| Dimension | Personal Agent | Work Agent | **PAD** |
|---|---|---|---|
| Rôle | assiste une personne en continu | accomplit un travail précis | **fait tourner un système de travail** |
| Entrée principale | préférences, flux, événements | tâche et matériel explicites | **informations, objectifs, tâches, retours** |
| Initiative | élevée | centrée sur la tâche | **intake proactif, exécution gouvernée** |
| Exécution | suit et agit seul | un ou plusieurs pas | **plusieurs agents, plusieurs nœuds** |
| Contexte | mémoire personnelle longue | contexte de la tâche | **état des tâches et historique du projet** |
| Décision | l'agent juge beaucoup | l'utilisateur définit l'objectif | **l'humain tient la direction, l'agent assiste** |
| Valeur | vous ne manquez rien | le travail est fait | **transformer l'information en production, en continu** |

```
Information · Personal Agent
  collecte, filtre et classe en continu ce qui compte
        ↓  « voici quelques choses qui méritent votre attention »
Décision · vous + PAD
  juge ce qui vaut la peine ; fixe objectifs et priorités
        ↓  « fais-le comme ceci ; voici les critères d'acceptation »
Exécution · Work Agents
  développement, contenu, recherche, déploiement, distribution
        ↓  « fini, voici les preuves, voici où ça a bloqué »
Retour · état des tâches et artefacts
  traces d'exécution, résultats, problèmes, prochain ajustement
```

**Le point porteur est cette deuxième flèche.** Un agent pleinement autonome confond « j'ai trouvé quelque chose qui mérite l'attention » et « j'ai décidé d'agir maintenant » : il voit une nouveauté, la déploie, la modifie, ajoute des fonctions, et produit un tas que personne n'a demandé. PAD sépare les deux — architecturalement, pas par un prompt demandant de vérifier d'abord.

## Trois autres choses

**Les tâches sont des objets durables, pas des conversations ponctuelles.** L'interaction typique d'un Work Agent est « énoncer une tâche → exécuter → renvoyer → fini ». PAD sort la tâche de la session : elle démarre à l'heure et le hub gère état et résultats. Ce que vous voyez est un flux continu de travail, pas une série de conversations coupées.

**Exploration et production exigent des critères différents.** L'exploration veut une couverture large et un vrai gain d'information ; la production veut un périmètre borné et un résultat contrôlable. Les mélanger produit l'échec classique — une idée surgit en cours d'exécution, les règles et le périmètre gonflent, l'objectif réel dérive. PAD leur donne des points d'entrée, des configurations de nœuds et des critères d'acceptation différents.

**L'actif qui s'accumule est le système de travail.** La capacité d'un agent ordinaire vient du modèle ; changez le modèle, la capacité se réinitialise. PAD accumule affectations de nœuds, enregistrements de tâches, expérience de projet, façons d'exécuter réutilisables. Le modèle est le moteur ; le système est la voiture.

---

## Démarrage rapide

```bash
git clone https://github.com/Liam-magicbook/pad.git && cd pad
./quickstart.sh
```

En moins de trois minutes, il crée un virtualenv, installe les dépendances, génère une clé, démarre le serveur, démarre un nœud local, envoie une vraie tâche et imprime le rapport.

![Démarrage rapide](docs/preview/quickstart.jpg)

À la fin vous obtenez une URL MCP ; collez-la dans ChatGPT et c'est fini. Arrêt : `./quickstart.sh --stop`.

Prérequis : Python 3.10+ et la possibilité de créer un venv. Pas de Docker, pas de serveur de base de données.

### Ce que vous obtenez

```
/pad/<KEY>/mcp        pour le cerveau (ChatGPT / Claude) — 9 outils de tâche
/pad/<KEY>/api/...    pour le démon de chaque machine — enregistrer / prendre / rapporter
```

Le modèle ne se souvient pas de ce qu'il a fait hier, de quelle machine est occupée, ni où une tâche est bloquée. Il ne peut pas, et il ne devrait pas. PAD garde cela dans SQLite ; le modèle pose une question — *quelle est la situation* — reçoit un résumé et continue.

Vous dites « produis un rapport », le modèle appelle `get_digest` une fois et voit :

```
nodes:
  mac-1       running 2 [build:12min, test:0min]
    caps: build.heavy, test.suite
  vps-1       idle
    caps: scrape.batch
active (2):
  t-20261102-a3f2   running   build.heavy   6 cibles recompilées
done today: 7
```

Il envoie alors vers `scrape.batch` parce que `vps-1` se trouve libre — et il **n'a jamais appris un nom de machine**.

![digest](docs/preview/digest.jpg)

---

# Détails techniques

Tout ce qui suit explique comment c'est construit. Si vous voulez seulement l'utiliser, la partie au-dessus suffit.

## Connecter un cerveau

MCP est la seule surface externe. Le cerveau reçoit neuf outils ; il n'y a ni `run`, ni `shell`, ni `machine_id` :

```
get_digest(recent_limit=10)          état de la flotte : qui est up, quoi tourne, fait aujourd'hui
submit_task(capability, goal, …)     envoyer une tâche (fichiers optionnels)
get_task(task_id)                    une tâche : statut + flux de rapports + artefacts
get_artifact(task_id, name, offset)  lecture paginée d'un artefact
get_input_file(task_id, name)        ouvrir un fichier envoyé avec la tâche
close_task(task_id, reason)          annuler (tue le groupe de processus local)
probe_file_upload(files)             vérification de bout en bout du chemin pièce jointe
get_artifact_delivery / observe      lien signé et échantillonnage d'images (optionnel)
```

![Surface d'outils MCP](docs/preview/mcp.jpg)

**ChatGPT** : Settings → Connectors (Developer mode) → Create → MCP Server URL
`http://<hôte>:8790/pad/<KEY>/mcp` → Scan tools → confirmez l'apparition des outils.

**Claude Code / tout client MCP** :

```json
{
  "mcpServers": {
    "pad": { "type": "http", "url": "http://127.0.0.1:8790/pad/<KEY>/mcp" }
  }
}
```

Fonctionner sur localhost ne veut pas dire fonctionner depuis internet. Pour un téléphone ou un accès externe, voir [Déploiement](#déploiement).

## Un seul PAD, trois cerveaux

C'est le mécanisme derrière « l'humain garde la décision ».

Le mot « agent » désigne deux choses différentes, et les confondre est ce qui déforme la plupart des produits d'assistant personnel.

- **Work agent** — autorité bornée. Vous énoncez un résultat ; il termine dans une enveloppe déclarée.
- **Personal agent** — autorité permanente. Il décide quoi faire, quand, et avec quelles ressources.

L'industrie suppose qu'il faut deux systèmes. La réponse de PAD : **ce n'est qu'une question de quel client occupe l'emplacement du cerveau, pas une question d'architecture.** L'emplacement est remplaçable, et le hub s'en moque.

| Qui occupe l'emplacement | Autorité | Entrée |
|---|---|---|
| Modèle de chat | Délégation : vous demandez en prose, le modèle décide combien de tâches et quelles capacités | MCP `/mcp`, `created_by=chatgpt` |
| Vous | Directe : vous nommez une capacité et un objectif en ligne de commande | `padctl submit`, `created_by=human` |
| Planificateur | Périodique : soumet le même pipeline chaque jour | minuteur interne, `created_by=daily-auto-timer` |

Les trois passent par le **même** `submit_task`, écrivent dans la **même** table `tasks`, franchissent le **même** portail d'acceptation et apparaissent dans le **même** digest. Rien en aval ne branche sur `created_by`. **Le cerveau n'est pas un composant, c'est un rôle**, et tout client satisfaisant le protocole peut l'occuper.

Deux mois en production, la distribution réelle des trois cerveaux sur un hub :

| | Modèle de chat | Vous | Planificateur |
|---|---|---|---|
| tâches | 854 | 39 | 36 |
| terminées | 680 | 36 | 19 |
| échouées | 111 | 2 | 17 |
| couverture des preuves | 96% | 47% | 100% |
| durée médiane | 14,6 min | 0,7 min | 341 min |
| heures-machine | 632,9 | 6,9 | 194,4 |
| longueur médiane d'objectif | 301 car. | 131 car. | 611 car. |
| avec critères d'acceptation | 98% | 79% | 100% |
| capacités touchées | 12 | 8 | 1 |

**Trois directions se séparent d'un coup :**

- Le travail confié au modèle est **spécifié** : objectifs de 301 caractères, 98 % portant des critères, réparti sur 12 capacités.
- Le travail que vous soumettez à la main est **dirigé** : objectifs de 131 caractères, 79 % avec critères, médiane sous la minute, 6,9 heures-machine au total sur deux mois.
- Le planificateur est l'autre extrême : 1 capacité, les objectifs les plus longs, un **taux d'échec de 47 %** — un pipeline fixe sans surveillance, où personne ne remarque que sa cible a bougé.

**Basculer est gratuit et sans migration.** Sur 46 jours actifs, 10 contiennent des tâches du modèle de chat et de vous. Le travail bien compris dont le résultat tient en une commande, vous l'envoyez vous-même ; le travail demandant recherche, décomposition ou une séquence de soumissions va au modèle.

**L'autorité est une question de degré, pas deux systèmes.** Ce qui sépare un work agent prudent d'un personal agent lâché n'est ni un bac à sable ni un jeu d'outils — c'est **quelle part de l'enveloppe est fixée à l'avance**. Une interaction `padctl` fixe capacité, objectif et critères, chaque champ décidé par un humain. Le modèle de chat a les mêmes neuf outils et le même plafond.

**Plus d'autonomie est une ligne de YAML, pas un changement d'architecture.** Aucune ligne du hub ne change ; ce qui change est **quelles capacités vous déclarez**. Cela garde la délégation explicite et révocable au lieu de l'enterrer dans un prompt.

**Mais l'autonomie a un coût.** Le même mécanisme qui rend le basculement gratuit rend la sur-délégation facile : un modèle tenant une large liste de capacités soumettra volontiers un objectif qu'aucune capacité ne peut satisfaire, et le hub enregistre docilement un échec. Environ la moitié de la taxonomie d'échec a cette forme. **Si l'autorité de routage avait été donnée au modèle, ce système n'aurait pas pu produire de taxonomie d'échec du tout** — il n'y aurait eu aucun endroit unique consignant ce qui a mal tourné.

## Coordination multi-machines sans coordinateur

L'objection naturelle à un hub est que centralisé signifie goulot d'étranglement. PAD est
**centralisé en état, décentralisé en exécution**, et cette distinction explique pourquoi de nombreuses
machines coopèrent sans processus planificateur.

Le hub n'a aucun travailleur de file et n'exécute rien. À chaque interrogation il répond à deux questions : **y a-t-il du travail correspondant aux capacités que j'ai déclarées**, et **quel est l'état du travail que je détiens**. Chaque nœud interroge à son propre rythme et ne prend que s'il a un emplacement libre.

**Il n'y a aucune étape d'affectation.** Le travail est **pris**, pas envoyé. La table de routage du hub est un ensemble de déclarations, pas un ensemble de décisions. Quatre propriétés en découlent :

- **Pas de planificateur, pas de consensus.** Le travail est pris dans une table indexée par capacité ; le hub n'a besoin d'aucune vue globale de charge. Ajouter une machine est un enregistrement ; en retirer une est un timeout.
- **La contre-pression est automatique.** Un nœud déclarant `concurrency: N` ne prendra pas le (N+1)-ième. La file grandit dans la base — l'endroit où elle est visible.
- **L'échec est local.** Un nœud qui meurt en cours de tâche laisse la tâche en `running` jusqu'à expiration du bail ; les rapports sont en ajout seul et idempotents, donc le détenteur suivant reprend depuis le registre.
- **L'enregistrement de coordination est l'enregistrement d'audit.** Chaque prise, rapport et artefact est une ligne. La taxonomie d'échec n'a demandé aucune instrumentation supplémentaire.

Le coût est réel et dit d'emblée : un hub est un point unique de coordination, et un redémarrage abandonne la supervision des processus des tâches en cours. Pour un opérateur avec quelques machines, l'échange est manifestement correct ; pour du multi-tenant il ne l'est pas, et PAD ne le prétend pas.

## Ajouter une machine

Une machine = un nœud = un YAML. **Aucun changement de code PAD.**

```bash
# sur la nouvelle machine
export PAD_HOME=$HOME/pad-ld
BASE=http://<hôte>:8790/pad/<KEY>

mkdir -p "$PAD_HOME"
curl -fsS "$BASE/agent.bundle.tgz" | tar xz -C "$PAD_HOME"
cd "$PAD_HOME"
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

cp ld_config.node.yaml.example ld_config.yaml
vi ld_config.yaml                 # node_id / capabilities / pad_url
.venv/bin/python ld_agent.py ld_config.yaml
```

L'`agent.bundle.tgz` récupéré est le démon de nœud et les prompts **actuels** du serveur. Aucun clonage, et pas de « cette machine fait tourner une logique d'il y a trois mois ». Quand le squelette du serveur change, les nœuds signalent une empreinte non concordante à l'enregistrement et une commande le récupère à nouveau (sans toucher à votre `ld_config.yaml`).

Le protocole d'adhésion complet, la liste d'auto-vérification et les pièges connus sont dans [JOIN.md](JOIN.md).

### Deux façons de déclarer une capacité

```yaml
capabilities:

  # 1. déterministe : appeler une commande
  scrape.batch:
    desc: "scrape par lots, lecture seule"
    cmd: "cd ~/projects/scraper && python3 crawl.py --out $PAD_WORK_DIR/data.json"
    timeout_min: 120

  # 2. demande du jugement : confier à un CLI agent local, sans interface
  build.heavy:
    desc: "tâches de code lourdes"
    cmd: "python3 /opt/pad-ld/harness/run_harness.py opencode"
    timeout_min: 240
    concurrency: 2
```

`run_harness.py` traduit une tâche en l'invocation d'un CLI — **changer d'exécuteur est un argument** : `opencode` / `claude` / `codex`. PAD ne sait pas quel modèle vous faites tourner ; il envoie « ce qui doit arriver », et chaque nœud s'adapte, donc les machines peuvent différer.

Dans les deux cas, le contrat est le même : terminé ou échoué, le nœud écrit `$PAD_WORK_DIR/result.json` avec `done`/`failed`, un rapport en prose et la preuve par critère. **Pas de `result.json` signifie pas terminé**, même avec un code de sortie 0.

### À se demander avant d'ajouter une capacité

> Si deux machines faisaient cela en même temps, entreraient-elles en collision ?

| Réponse | Que faire |
|---|---|
| **Oui** (état de connexion partagé, même fichier de sortie, même ressource) | **Une seule** peut la déclarer |
| **Non** (scraping, tests, lectures pures) | Déclarer le **même nom** sur plusieurs nœuds — c'est la montée en charge horizontale |

Ne renommez jamais une capacité conflictuelle pour contourner la règle. Le hub la traiterait comme un autre travail et en enverrait une à chacun.

## Principes de conception : quel échec chaque choix évite

Les mécanismes sont dans la section 3 de l'article ; voici le **pourquoi**.

**L'état doit survivre au processus qui l'a produit.** Deux processus meurent toujours : la fenêtre de contexte et l'exécuteur. La réponse dominante est d'agrandir le premier — transcripts plus longs, résumés, recherche — en traitant le contexte comme du stockage. Ce n'est pas du stockage : pas de politique d'éviction, pas de schéma, pas de langage de requête, pas de partage, pas de piste d'audit, et il est le plus susceptible d'être tronqué exactement quand la tâche s'allonge. Nous plaçons chaque fait durable à la couche au temps moyen entre pannes le plus long : un processus, un fichier SQLite, en ajout seul.

**L'interface doit être plus étroite que la capacité.** Les harness grandissent en ajoutant des outils, chacun localement raisonnable et globalement coûteux. Neuf n'est pas un budget choisi pour l'élégance ; c'est l'ensemble qui survit à « le cerveau a-t-il besoin de ceci, ou l'exécuteur l'a-t-il déjà ? ». Tout ce qui agit sur le monde — shell, système de fichiers, navigateur, réseau, identifiants — appartient à l'exécuteur. Le cerveau ne reçoit que des opérations sur le **travail**.

**Router sur le quoi, jamais sur le où.** Dès qu'un envoi nomme un hôte, trois choses deviennent vraies d'un coup : l'émetteur doit connaître la flotte, la flotte ne peut plus changer, et la piste d'audit enregistre une machine plutôt qu'une intention.

**Le bord connaît mieux son environnement que le centre.** Un envoi qui spécifie des étapes prétend que l'émetteur connaît l'environnement d'exécution. Cette prétention est généralement fausse : si un CLI a besoin de `--skip-git-repo-check`, si le dépôt est propre, si un cookie a expiré, s'il reste de la place sur le disque — tout n'est visible qu'au bord.

**La preuve est un artefact, pas une assertion.** Un code de sortie suffit quand la commande et l'objectif sont la même chose, et ne sert à rien pour « produire un résumé vérifié ». Le système qui a lancé la commande est aussi le moins capable de juger si l'objectif est atteint.

**L'échec est un résultat de première classe.** Dans la plupart des harness, l'échec est une exception. Dans le travail de longue haleine cette forme est fausse — une exécution de quatre heures qui découvre que l'objectif ne peut pas être atteint tel qu'énoncé a produit une conclusion, et la jeter comme une erreur jette la conclusion.

**Ce que nous n'avons délibérément pas construit** (raisonnement complet en annexe A de l'article) : pas de planificateur dans le hub ; pas de bus de messages ni de base vectorielle ; pas de dialogue en cours de tâche ; pas de multi-tenant ; pas de modèle dans le chemin de routage.

## Déploiement

Localhost ne suffit pas pour un téléphone, un autre réseau ou le cloud de ChatGPT. Trois couches :

```
internet → reverse proxy (nginx/caddy, TLS) → 127.0.0.1:8790 (uvicorn)
```

Les choses qui mordent (chacune a coûté une session de débogage) :

| Élément | Note |
|---|---|
| **`PAD_ALLOWED_HOSTS`** | Liste blanche DNS-rebinding de MCP, comparée **littéralement à l'en-tête `Host`, port inclus**. Un hôte absent renvoie 421 alors que REST renvoie encore 200 — atroce à diagnostiquer |
| **`PAD_KEY`** | C'est *l'authentification*. Elle est dans l'URL, donc traitez-la comme un secret révocable de longue durée. Ne la collez pas en public |
| **Limite de corps du proxy** | Les valeurs par défaut sont petites ; les tâches avec fichiers renvoient 413. Réglez `client_max_body_size 32m` sur la location `/pad/` |
| **Ne redémarrez pas le serveur** | `systemctl restart pad` tue les groupes de processus des tâches en cours. Redémarrez dans une fenêtre calme |

```bash
# serveur
PAD_KEY=$(cat data/.pad_key) \
PAD_DB=/var/lib/pad/pad.db \
PAD_ALLOWED_HOSTS=pad.example.com,localhost,127.0.0.1 \
.venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8790
```

Les unités systemd sont dans `deploy/pad.service` et `deploy/pad-node.service` (Linux), plus `deploy/com.pad.ld.plist` (macOS).

## Envoyer des fichiers avec une tâche

Le modèle ne doit pas fourrer des mégaoctets de base64 dans son contexte. Deux chemins :

**1. `files` (pièces jointes ChatGPT)** — l'utilisateur joint un fichier ; ChatGPT le convertit via `_meta["openai/fileParams"]` en `{download_url, file_id, mime_type, file_name}`, et le serveur récupère les octets **pendant cet appel**. Le modèle ne touche jamais au contenu.

**2. `input_files`** — quand l'appelant a déjà les octets : `{name, mime_type, data_base64}`.

Les deux partagent un même chemin de validation (8 fichiers par tâche, ≤10 MiB chacun, ≤15 MiB au total). Le nœud voit les fichiers d'origine plus `manifest.json` avec sha256 dans `inputs/`. Tout type : images, PDF, Office, CSV/JSON/Markdown, archives, audio, vidéo, sous-titres.

Sans écrire du base64 à la main :

```bash
./padctl.py submit --capability build.heavy --goal "refais le plan selon ce rapport" \
  --file ~/Downloads/rapport-trimestriel.pdf --file ~/Downloads/donnees.xlsx
./padctl.py inputs t-20261102-xxxxxx          # quels fichiers porte cette tâche
./padctl.py input  t-20261102-xxxxxx --out .  # télécharger et vérifier
```

**`download_url` n'est valide que pendant cet appel**, donc les octets doivent être récupérés de façon synchrone. Ce chemin est traité comme une SSRF : https seulement, la résolution DNS rejette les plages privées/bouclage/réservées, **chaque saut de redirection est revalidé**, et les lectures sont comptées contre la limite de taille.

## Arborescence

| Fichier | Rôle |
|---|---|
| `server.py` | Serveur (FastAPI + MCPServer + SQLite) ; les deux portes |
| `db.py` / `schema.sql` | Couche d'état |
| `ld_agent.py` | Démon de nœud sur chaque machine |
| `harness/run_harness.py` | Traduit une tâche en appel sans interface opencode / claude / codex |
| `harness/run_script.py` | Enveloppe une commande dans le contrat PAD (travail déterministe) |
| `harness/prompt.md.tmpl` | Prompt de l'exécuteur. Changez les règles uniquement ici |
| `padctl.py` | CLI humaine (gère aussi `--file`) |
| `pad_mcp.py` | Client de débogage via MCP (même chemin que le cerveau) |
| `quickstart.sh` | Démarrage en une commande |
| `contract.md` | Le contrat : base d'envoi, format de rapport, discipline d'exécution |
| `JOIN.md` | Protocole d'adhésion + auto-vérifications + liste des pièges |
| `CHANGELOG.md` | Changements de comportement ; à consulter d'abord en déboguant |
| `docs/make_preview.py` | Régénérer le GIF d'accueil (sortie réelle → images → ffmpeg) |
| `paper/` | Rapport système (`pad.pdf`, ICML Template) + mesures agrégées uniquement |
| `tools/collect_stats.py` | Récupérer les détails de tâches d'un PAD vivant pour reproduire les chiffres de l'article |
| `README.{zh,fr,ja,ru}.md` | Versions chinoise / française / japonaise / russe |

## Développement

```bash
.venv/bin/python -m unittest discover -s tests -v   # tests unitaires
bash tests/e2e_input_files.sh                       # de bout en bout : serveur + nœud temporaires, vrai chemin
```

Les images de démonstration ne sont pas assemblées à la main ; elles capturent la sortie réelle de cette machine puis la photographient. Relancez après avoir changé le format de sortie :

```bash
./quickstart.sh          # un PAD local doit tourner
.venv/bin/python docs/make_preview.py
```

## Article

`paper/pad.pdf` (format ICML Template, 8 pages de corps + 5 d'annexe) transforme ce qui précède en
rapport système citable : définition du problème, relation aux travaux harness / long-horizon /
delegation, architecture, principes de conception et mesures. L'annexe développe le schéma de la base,
la machine à états des tâches, chacun des neuf contrats d'outil et les scripts de reproduction.

Les mesures viennent de deux mois de journaux réels de ce système en production :

| Métrique | Valeur |
|---|---|
| Nœuds / capacités | 6 machines, 20 capacités, les CLI agent de 3 éditeurs |
| Tâches réelles (hors auto-vérifications) | 938, 835 heures-machine |
| Tâches longues | 18,7% au-delà d'une heure, 25 au-delà de 6 heures, la plus longue 42 heures |
| Preuve par critère sur les tâches terminées | 93% |
| Tâches échouées livrant tout de même un rapport structuré | 42% |
| Cause dominante d'échec | 64/132 sont des défaillances d'environnement d'exécution (pas le modèle qui échoue) |

Cette dernière ligne est ce que tout le design défend : **la plupart des échecs se produisent dans l'environnement, pas dans le raisonnement.** Un harness à boucle unique voit « l'appel d'outil a cassé » ; PAD voit « cette capacité sur cette machine est cassée », et c'est la seconde qui est réparable.

Données et scripts de reproduction : `paper/data/aggregates.json` et `tools/collect_stats.py`. Le dump brut contient les objectifs réels de l'opérateur et n'est pas dans le dépôt.

## Lacunes connues

- L'authentification est une clé dans l'URL, pas d'OAuth. Passer à OAuth signifie changer la couche devant le proxy ; le contrat reste
- Une tâche qui plante en cours de `running` (pas de rapport terminal) n'est pas reprise automatiquement ; il faut un reaper de tâches périmées
- La limite de 10 MiB par fichier vient du tamponnage en mémoire du serveur ; des centaines de Mo demanderaient des écritures par morceaux
- Les entrées non-image ne sont pas analysées dans PAD (pas d'extraction de texte PDF, pas de conversion Office) ; l'exécuteur du nœud les lit avec les outils locaux
- La publication en workspace ChatGPT Business/Enterprise n'est pas implémentée ; seulement le developer mode personnel

---

MIT License
