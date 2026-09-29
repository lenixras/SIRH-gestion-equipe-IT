# Gestion d'équipe technique

Un petit site pour piloter une équipe technique : qui fait quoi, ce que chacun traite vraiment
(interventions, tickets, projets), si les délais sont tenus, et où en sont les projets.
Vous n'avez rien à installer sur votre poste : on y va avec un lien, on se connecte, c'est tout.

## À quoi ça sert

- **Vous êtes technicien** : vous avancez vos tâches (Kanban), vous notez ce que vous dépannez
  (« intervention de support »), vous voyez votre profil se remplir au fil du travail réel.
- **Vous êtes manager** : vous préparez la revue d'équipe sans ressaisir de chiffres — l'activité,
  le respect des délais et la difficulté traitée par chacun sont calculés automatiquement.
- Les tickets du helpdesk (GLPI) sont **importés** : pas besoin de les ressaisir ici.

Tout le monde travaille sur **les mêmes données** : ce que voit l'écran, l'import des tickets et
les assistants connectés lisent et écrivent la même base, avec un journal de chaque modification.

## Démarrer (administrateur, une fois)

```bash
./sirh install    # dépendances + base de données + premier compte manager
./sirh start      # l'application écoute sur http://localhost:8000
./sirh stop       # l'arrêter
```

À l'installation, on vous demande l'email et le mot de passe du premier manager (sinon un mot de
passe est généré et affiché une seule fois — notez-le).

## Se connecter

Ouvrez `http://localhost:8000`, puis `Se connecter` avec votre email et mot de passe.
La session dure 12 heures ; le bouton **Déconnexion** est en haut à droite.
Le manager peut créer les comptes des techniciens dans l'écran « Équipe ».

## Les écrans (menu du haut)

| Écran | Ce qu'on y fait |
|---|---|
| **Dashboard compétence** | La vue principale : un tableau par personne (activité support + tickets + projets, % de délais tenus, % de réussite, escalades) avec des graphiques. On peut filtrer par **semaine / mois / année** et chercher **une personne** par son nom. L'écran se recharge tout seul dès que quelqu'un enregistre quelque part. |
| **Équipe** | La liste des personnes, leur rôle, leur identifiant dans l'outil de tickets, et la fiche de chacun (ses tâches, ses notes, son historique, ses objectifs). |
| **Projets** | Les projets, leurs jalons (les retards sont signalés), et qui est affecté à quoi. |
| **Kanban** | Vos tâches en colonnes : on déplace une carte « À faire → En cours → Termine » avec la souris. Un technicien ne bouge que ses cartes. |
| **Support** | Déclarer une intervention qui n'a pas de ticket : « coupure réseau chez X », signalée à 9 h, résolue à 11 h, avec la solution. En haut, le résumé par technicien (nombre, réussite, délais moyens). Une fiche peut être marquée **Bloqué — Escalade** quand elle dépasse votre compétence. |
| **KPI** | Les chiffres des tickets importés : délais moyens (prise en charge, résolution), par personne, par catégorie, par mois, avec le bouton **Synchroniser** pour récupérer les tickets de GLPI. |
| **Catégories** *(manager)* | Le classement des tâches : Type → Classe → Niveau (N1/N2/N3) → délai acceptable (SLA). Exemple : « Intervention / Coupure réseau / N1 / 30 min ». C'est cette grille qui colore tous les délais en vert ou rouge, ici comme dans les tickets. |
| **Rôles** *(manager)* | Qui a le droit de faire quoi : créer des rôles, cocher leurs permissions. |

## Comprendre les couleurs

- **Vert** = la tâche a été finie dans son délai (SLA de la catégorie).
- **Rouge** = hors délai, ou déjà au-delà du délai si elle est encore ouverte.
- **Bleu** = encore ouverte, mais dans les temps.
- Sur le Kanban et le dashboard, la couleur de gauche de chaque carte/valeur vient de la catégorie.

## Bonnes habitudes

- Une difficulté qui vous échappe → statut **Bloqué — Escalade** (le manager la voit sur votre fiche).
- Un dépannage sans ticket → écran **Support**, sinon votre activité sera sous-estimée.
- Les tickets GLPI se récupèrent avec le bouton **Synchroniser** (ou chaque nuit si l'administrateur
  l'a programmé) : un ticket sans catégorie est rangé en « Incident » ou « Demande » automatiquement.
- Le **niveau** (N1/N2/N3) décrit la difficulté de la **tâche**, jamais la personne : le dashboard
  montre au contraire ce que la personne a réellement tenu, et si elle monte en gamme.

## Connecter GLPI (l'outil de tickets)

GLPI n'est pas obligatoire : la plateforme tourne sans, et on peut l'ajouter à tout moment.
À l'installation (`./sirh install`), une question demande si on veut installer aussi le vrai GLPI 11
(≈ 450 Mo d'images Docker, deux conteneurs : GLPI + MariaDB). Répondez `o` et les jetons d'API sont
créés tout seuls ; répondez `N` et vous pourrez le faire plus tard avec les commandes ci-dessous.

**Cas 1 — GLPI à installer ici (le plus simple) :**

```bash
./sirh glpi-real        # demarre GLPI 11 sur http://127.0.0.1:8080 (glpi / glpi)
./sirh glpi-real-keys   # active l'API et remplit GLPI_* dans .env (verifie par initSession)
./sirh stop && ./sirh start   # pour que l'app prenne en compte le .env
```

Puis à l'écran **KPI**, bouton **Synchroniser** : les tickets arrivent avec leurs délais.

**Cas 2 — votre entreprise a déjà un GLPI :** dans le fichier `.env`, renseigner les trois valeurs
fournies par l'administrateur GLPI (Menu → Régler → API ; un compte de service avec un token) :

```
GLPI_URL=https://glpi.exemple.fr
GLPI_TOKEN=<app-token>
GLPI_PASSWORD=<user-token du compte de service>
```

Redémarrer (`./sirh stop && ./sirh start`), puis **Synchroniser** à l'écran KPI. Les techniciens GLPI
sont rattachés aux personnes du SIRH par leur **identifiant GLPI** (renseigné dans « Équipe ») — à
défaut, par le nom.

Pour importer chaque nuit sans se connecter à l'écran :
`0 2 * * * cd <projet> && python3 -m scripts.manage import-kpi --glpi`.

Sans GLPI sous la main (démo, tests), le simulateur joue le rôle du helpdesk : `./sirh glpi`.


## Pour aller plus loin (techniciens d'infra)

- L'application expose aussi une **API JSON** (mêmes accès, mot de passe en HTTP Basic) et un
  **serveur MCP** (`/mcp/`, 16 outils) pour les assistants connectés — mêmes droits et même
  journal d'audit que les écrans ; les écritures n'y sont ouvertes que si `MCP_WRITE=1`.
- `GET /api/competences/analyse` renvoie en JSON exactement le tableau du Dashboard compétence.
- Développement, simulateur GLPI, tests, configuration : voir le fichier `14-pilotage-equipe-mcp.md`
  et les sections techniques de l'historique — `python3 -m pytest tests -q` (61 tests) valide
  l'ensemble ; le CSS se recompille avec `./sirh css` après une modification de gabarit.

## Hors périmètre

Ce n'est pas un outil de ticketing (le helpdesk reste GLPI, on n'importe que ses chiffres),
ni de la paie, ni des congés, ni un Gantt complet.
