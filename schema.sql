-- Mini-SIRH technique : 6 tables metier + audit + vues d'agregats.
-- Idempotent (IF NOT EXISTS) : applique au demarrage de l'app, pas de migrations a gerer.

CREATE TABLE IF NOT EXISTS persons (
  id            serial PRIMARY KEY,
  name          text NOT NULL,
  email         text NOT NULL UNIQUE,
  title         text,
  role          text NOT NULL DEFAULT 'tech',
  password_hash text NOT NULL,
  active        boolean NOT NULL DEFAULT true,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- Id de l'utilisateur technicien dans l'outil de tickets (GLPI search/Ticket colonne 5).
-- Renseigne a la creation ou complete plus tard ; le rapprochement d'import le privilegie au nom.
ALTER TABLE persons ADD COLUMN IF NOT EXISTS glpi_id int;
CREATE UNIQUE INDEX IF NOT EXISTS idx_persons_glpi_id ON persons (glpi_id) WHERE glpi_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS skills (
  id         serial PRIMARY KEY,
  person_id  int NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
  name       text NOT NULL,
  level      int NOT NULL CHECK (level BETWEEN 1 AND 5),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (person_id, name)
);

-- Une competence est definie pour une personne DANS une categorie technique : la meme grille que
-- les interventions support, les projets et les tickets (helpdesk, reseau, serveur, ...). L'ancien
-- `name` libre (ex. 'stockage') migre vers la categorie la plus proche, sinon « Autre », et un
-- niveau par (personne, categorie) est garde (le plus eleve).
ALTER TABLE skills ADD COLUMN IF NOT EXISTS category text REFERENCES categories(key);
-- le mapping des anciennes competences libres (ex. 'stockage') n'est possible que si la colonne
-- name existe encore ; sinon (schema deja migre) on ne touche a rien
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name='skills' AND column_name='name') THEN
    UPDATE skills s SET category = coalesce(
      (SELECT c.key FROM categories c WHERE lower(c.key) = lower(s.name) OR lower(c.label) = lower(s.name)),
      CASE WHEN lower(s.name) = 'stockage' THEN 'serveur' END,
      'Autre') WHERE s.category IS NULL;
  END IF;
END $$;
DELETE FROM skills s USING (
  SELECT id, row_number() OVER (PARTITION BY person_id, category ORDER BY level DESC, id) AS rn
  FROM skills
) r WHERE s.id = r.id AND r.rn > 1;
ALTER TABLE skills ALTER COLUMN category SET NOT NULL;
-- CASCADE : l'index supportant l'ancien UNIQUE (person_id, name) disparait avec la colonne
ALTER TABLE skills DROP COLUMN IF EXISTS name CASCADE;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'skills'::regclass
                 AND conname = 'skills_person_category') THEN
    ALTER TABLE skills ADD CONSTRAINT skills_person_category UNIQUE (person_id, category);
  END IF;
END $$;

-- Historique date des competences (revue trimestrielle, ecart niveau)
CREATE TABLE IF NOT EXISTS skills_log (
  id             serial PRIMARY KEY,
  person_id      int NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
  skill          text NOT NULL,
  level          int NOT NULL,
  previous_level int,
  note           text,
  source         text NOT NULL DEFAULT 'dashboard',
  at             timestamptz NOT NULL DEFAULT now()
);

-- Arborescence des taches : taxonomie commune a tous les types de tache (Support, Ticketing,
-- Projets). Chaque ligne = (Type, Classe, Niveau, SLA). Une fiche reference une CLASSE (via la cle
-- de sa ligne de niveau minimal) et porte son propre NIVEAU ; le SLA se lit sur (classe, niveau).
-- MTTR > SLA => hors delai (rouge) ; MTTR <= SLA => tenu (vert).
CREATE TABLE IF NOT EXISTS categories (
  key         text PRIMARY KEY,
  type        text NOT NULL,
  classe      text NOT NULL,
  niveau      text NOT NULL CHECK (niveau IN ('N1','N2','N3')),
  sla_minutes int  NOT NULL CHECK (sla_minutes > 0),
  couleur     text
);

-- Migration d'une base existante : l'ancien vocabulaire plat (key + label) devient des classes
-- rangees dans les 3 types (Intervention / Administration / Deploiement), les fiches existantes
-- restent en N1, et chaque classe recupere ses niveaux N2/N3 avec un SLA par defaut (30/60/120).
ALTER TABLE categories ADD COLUMN IF NOT EXISTS type text;
ALTER TABLE categories ADD COLUMN IF NOT EXISTS classe text;
ALTER TABLE categories ADD COLUMN IF NOT EXISTS niveau text;
ALTER TABLE categories ADD COLUMN IF NOT EXISTS sla_minutes int;
ALTER TABLE categories ADD COLUMN IF NOT EXISTS couleur text;
ALTER TABLE categories ALTER COLUMN type SET DEFAULT 'Intervention';
ALTER TABLE categories ALTER COLUMN niveau SET DEFAULT 'N1';
ALTER TABLE categories ALTER COLUMN sla_minutes SET DEFAULT 30;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'categories' AND column_name = 'label') THEN
    UPDATE categories SET
      type = CASE WHEN key IN ('helpdesk','reseau','securite','incident','Autre') THEN 'Intervention'
                  WHEN key IN ('serveur','base-donnees','station') THEN 'Administration'
                  ELSE 'Deploiement' END,
      classe = label,
      niveau = 'N1',
      sla_minutes = 30;
  END IF;
END $$;
-- l'ancienne colonne label disparait une fois recopiee : plus rien ne s'en sert (ni fiches, ni
-- vues ; les vues d'analyse sont reconstruites juste apres dans ce meme fichier)
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'categories' AND column_name = 'label') THEN
    ALTER TABLE categories DROP COLUMN label CASCADE;
  END IF;
END $$;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'categories' AND column_name = 'niveau') THEN
    INSERT INTO categories (key, type, classe, niveau, sla_minutes)
    SELECT key || '-n2', type, classe, 'N2', 60 FROM categories WHERE niveau = 'N1'
    ON CONFLICT DO NOTHING;
    INSERT INTO categories (key, type, classe, niveau, sla_minutes)
    SELECT key || '-n3', type, classe, 'N3', 120 FROM categories WHERE niveau = 'N1'
    ON CONFLICT DO NOTHING;
  END IF;
END $$;
DO $$ BEGIN
  ALTER TABLE categories ADD CONSTRAINT categories_taxo UNIQUE (type, classe, niveau);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL; END $$;

-- Seed d'une base neuve : chaque classe de base porte ses 3 niveaux (SLA par defaut) et un couleur
-- (mise en evidence : Kanban, arborescence, dashboard) ; plus les exemples du besoin (Base de
-- donnees / Application). L'exemple « Application N2 1 h 30 » = 90. Les classes coexistent sans
-- couleur (affichage gris par defaut) dans une base deja peuplee.
WITH base(key, type, classe, couleur) AS (VALUES
  ('helpdesk',       'Intervention',   'Helpdesk / poste de travail', '#7c8ca3'),
  ('reseau',         'Intervention',   'Reseau',                      '#2f5d8a'),
  ('securite',       'Intervention',   'Securite',                    '#a12b2b'),
  ('incident',       'Intervention',   'Incident',                    '#b4552f'),
  ('demande',        'Intervention',   'Demande',                     '#2f7d8a'),
  ('serveur',        'Administration', 'Serveur / stockage',          '#1c6b3d'),
  ('base-donnees',   'Administration', 'Base de donnees',             '#6b5b95'),
  ('infrastructure', 'Deploiement',    'Infrastructure',              '#8a5b2f'),
  ('application',    'Deploiement',    'Application',                 '#2f8a8a'),
  ('Autre',          'Autre',          'Divers',                      '#94a3b8')),
level(v) AS (VALUES ('N1'), ('N2'), ('N3'))
INSERT INTO categories (key, type, classe, niveau, sla_minutes, couleur)
SELECT b.key || CASE l.v WHEN 'N1' THEN '' ELSE '-' || lower(l.v) END,
       b.type, b.classe, l.v,
       CASE l.v WHEN 'N1' THEN 30 WHEN 'N2' THEN 60 ELSE 120 END,
       b.couleur
FROM base b, level l
ON CONFLICT DO NOTHING;
UPDATE categories SET sla_minutes = 90 WHERE key = 'application-n2';
-- tickets GLPI sans categorie : l'import repliait sur `incident` / `request`, des noms qui ne sont
-- jamais une classe de la grille (donc SLA « — »). Ces deux noms deviennent des classes fourre-tout
-- (Incident / Demande, seedees ci-dessus avec leurs 3 niveaux) ; les lignes importees avant cette
-- regle sont rebranchees pour afficher enfin un SLA vert/rouge/bleu.
UPDATE kpi_raw SET category = 'Incident' WHERE lower(category) = 'incident';
UPDATE kpi_raw SET category = 'Demande'  WHERE lower(category) IN ('request', 'probleme');
INSERT INTO categories (key, type, classe, niveau, sla_minutes) VALUES
  ('base-donnees-n3', 'Administration', 'Base de donnees', 'N3', 120),
  ('serveur-n2',      'Administration', 'Serveur / stockage', 'N2', 60)
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS projects (
  id          serial PRIMARY KEY,
  name        text NOT NULL,
  status      text NOT NULL DEFAULT 'planned' CHECK (status IN ('planned', 'running', 'done', 'cancelled')),
  start_date  date,
  due_date    date,
  description text
);

-- Categorie technique et date de fin reelle : sans done_at, « realise dans les delais » serait un
-- mensonge (un projet termine apres son echeance serait compté dans les cles).
ALTER TABLE projects ADD COLUMN IF NOT EXISTS category text REFERENCES categories(key);
ALTER TABLE projects ADD COLUMN IF NOT EXISTS done_at   date;

-- Interventions techniques saisies a la main : ni projet avec echeance, ni ticket de l'outil de
-- tickets. MTTD = reported_at - created_at (delai de prise en charge), MTTR = resolved_at -
-- reported_at (temps d'intervention). resolved_at NULL = en cours. Le statut d'avancement reprend
-- celui d'une fiche de projet (Kanban) : une fiche resolue est forcement « terminee ».
CREATE TABLE IF NOT EXISTS support (
  id          serial PRIMARY KEY,
  person_id   int NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
  category    text NOT NULL REFERENCES categories(key),
  niveau      text NOT NULL DEFAULT 'N1' CHECK (niveau IN ('N1','N2','N3')),
  title       text NOT NULL,
  reported_at timestamptz NOT NULL,
  resolved_at timestamptz,
  status      text NOT NULL DEFAULT 'todo' CHECK (status IN ('todo','in_progress','blocked','done')),
  solution    text,
  created_at  timestamptz NOT NULL DEFAULT now()
);
-- niveau de difficulte de la fiche : requiert un technicien qui tient le niveau (migration des
-- fiches existantes : N1, le plus bas)
ALTER TABLE support ADD COLUMN IF NOT EXISTS niveau text NOT NULL DEFAULT 'N1'
  CHECK (niveau IN ('N1','N2','N3'));
ALTER TABLE support ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'todo'
  CHECK (status IN ('todo','in_progress','blocked','done','escalade'));
-- « Bloque — Escalade » : une personne rencontre une difficulte qui necessite une escalade
-- (statut ouvert, jamais compte comme traite tant qu'il n'est pas resolu).
DO $$ BEGIN
  ALTER TABLE support DROP CONSTRAINT support_status_check;
  ALTER TABLE support ADD CONSTRAINT support_status_check
    CHECK (status IN ('todo','in_progress','blocked','done','escalade'));
EXCEPTION WHEN undefined_object OR duplicate_object THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS idx_support_person ON support (person_id, reported_at DESC);
CREATE INDEX IF NOT EXISTS idx_support_cat    ON support (category, reported_at DESC);

CREATE TABLE IF NOT EXISTS assignments (
  id         serial PRIMARY KEY,
  project_id int NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  person_id  int NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
  role       text NOT NULL,
  status     text NOT NULL DEFAULT 'todo' CHECK (status IN ('todo', 'in_progress', 'done', 'blocked')),
  start_date date,
  due_date   date,
  progress   int NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Notes d'avancement horodatees
CREATE TABLE IF NOT EXISTS notes (
  id            serial PRIMARY KEY,
  assignment_id int NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
  author_id     int NOT NULL REFERENCES persons(id),
  progress      int CHECK (progress BETWEEN 0 AND 100),
  text          text NOT NULL,
  at            timestamptz NOT NULL DEFAULT now()
);

-- KPI bruts importes depuis l'outil de tickets (jamais calcules par un LLM)
CREATE TABLE IF NOT EXISTS kpi_raw (
  id          serial PRIMARY KEY,
  source      text NOT NULL,
  external_id text NOT NULL,
  person_id   int REFERENCES persons(id) ON DELETE SET NULL,
  category    text NOT NULL DEFAULT 'incident',
  niveau      text NOT NULL DEFAULT 'N1',
  priority    int,
  opened_at   timestamptz NOT NULL,
  taken_at    timestamptz,
  solved_at   timestamptz,
  imported_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source, external_id)
);

-- etat lisible du ticket dans l'outil source (GLPI : 1 nouveau 2/3 en cours 4 attente 5/6 resolu)
ALTER TABLE kpi_raw ADD COLUMN IF NOT EXISTS title  text;
ALTER TABLE kpi_raw ADD COLUMN IF NOT EXISTS status text;
ALTER TABLE kpi_raw ADD COLUMN IF NOT EXISTS niveau text NOT NULL DEFAULT 'N1';

CREATE TABLE IF NOT EXISTS objectives (
  id        serial PRIMARY KEY,
  person_id int NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
  period    text NOT NULL,
  title     text NOT NULL,
  target    numeric,
  achieved  numeric,
  UNIQUE (person_id, period, title)
);

-- Journal des ecritures (exigence MCP : ecriture gatee + trace)
CREATE TABLE IF NOT EXISTS audit_log (
  id        serial PRIMARY KEY,
  at        timestamptz NOT NULL DEFAULT now(),
  source    text NOT NULL,
  actor     text NOT NULL,
  action    text NOT NULL,
  entity    text NOT NULL,
  entity_id text,
  detail    jsonb NOT NULL DEFAULT '{}'::jsonb
);

-- ---------------------------------------------------------------- RBAC
-- Un role = un nom porte par persons.role ; des permissions nommees lui sont accordees.
-- Les regles de propriete de ligne (un technicien n'ecrit que sur sa fiche) restent dans le code :
-- une permission est un droit statique, la propriete depend de la ligne visee.
CREATE TABLE IF NOT EXISTS roles (
  name        text PRIMARY KEY,
  description text NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS permissions (
  key   text PRIMARY KEY,
  label text NOT NULL
);

CREATE TABLE IF NOT EXISTS role_permissions (
  role text NOT NULL REFERENCES roles(name) ON DELETE CASCADE,
  perm text NOT NULL REFERENCES permissions(key) ON DELETE CASCADE,
  PRIMARY KEY (role, perm)
);

INSERT INTO roles (name, description) VALUES
  ('manager', 'Administrateur : tout cree, tout modifier, gere les roles'),
  ('tech',    'Technicien : sa fiche, ses competences, ses affectations')
ON CONFLICT DO NOTHING;

INSERT INTO permissions (key, label) VALUES
  ('persons.manage',     'Creer / supprimer des personnes'),
  ('roles.manage',        'Attribuer les roles et les permissions'),
  ('projects.manage',     'Creer / modifier / supprimer des projets'),
  ('assignments.manage',  'Creer / supprimer des affectations et modifier celles des autres'),
  ('data.manage',         'Ecrire les competences, notes et objectifs de tout le monde'),
  ('support.manage',      'Enregistrer les interventions support de toute l equipe'),
  ('glpi.sync',           'Lancer la synchronisation GLPI'),
  ('audit.read',          'Lire le journal des ecritures')
ON CONFLICT DO NOTHING;

INSERT INTO role_permissions (role, perm)
SELECT 'manager', key FROM permissions
ON CONFLICT DO NOTHING;

-- Le role devient une reference : le CHECK initial ('manager'/'tech') interdirait d'en creer.
-- IF NOT EXISTS n'existe pas pour ADD CONSTRAINT, d'ou le DO.
ALTER TABLE persons DROP CONSTRAINT IF EXISTS persons_role_check;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conrelid = 'persons'::regclass AND conname = 'persons_role_fkey') THEN
    ALTER TABLE persons ADD CONSTRAINT persons_role_fkey FOREIGN KEY (role) REFERENCES roles(name);
  END IF;
END $$;

-- Sessions navigateur : le logout HTTP Basic est impossible (le navigateur garde les identifiants).
CREATE TABLE IF NOT EXISTS sessions (
  token      text PRIMARY KEY,
  person_id  int NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL DEFAULT now() + interval '12 h'
);

CREATE INDEX IF NOT EXISTS idx_sessions_person ON sessions (person_id);

CREATE INDEX IF NOT EXISTS idx_skills_log_person ON skills_log (person_id, at DESC);
CREATE INDEX IF NOT EXISTS idx_assignments_person ON assignments (person_id, status);
CREATE INDEX IF NOT EXISTS idx_notes_assignment ON notes (assignment_id, at DESC);
CREATE INDEX IF NOT EXISTS idx_kpi_person ON kpi_raw (person_id, solved_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log (at DESC);

-- ---------------------------------------------------------------- vues

-- Normalisation des noms de categorie/classe : support et ticketing (GLPI) doivent partager les
-- MEMES bases de connaissance. GLPI renvoie « Réseau », la grille porte « Reseau » ; sans ce
-- rapprochement (minuscules, accents neutralises, '_'/-'-'->espace), aucun ticket ne rejoint sa
-- classe et le SLA reste vide. IMMUTABLE, sans extension (aucun `unaccent` requis).
CREATE OR REPLACE FUNCTION cat_norm(t text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT trim(regexp_replace(
    replace(replace(translate(lower(coalesce(t,'')),
      'àáâãäåçèéêëìíîïðñòóôõöøùúûüýþÿ',
      'aaaaaaceeeeiiiiðnoooooøuuuuyþy'),
    '_',' '),'-',' '), '\s+',' ','g'))
$$;

-- MTTA = prise en charge - ouverture (temps de reaction)
-- MTTR = resolution - prise en charge (resolution seule, indicateur de diagnostic)
-- cycle = resolution - ouverture (duree de vie du ticket : le MTTD/MTTR du pilotage)
-- cycle_s est ajoute en FIN de liste : CREATE OR REPLACE VIEW n'accepte une nouvelle colonne
-- qu'en fin, donc l'upgrade d'une base existante fonctionne.
CREATE OR REPLACE VIEW v_kpi_summary AS
SELECT count(*)                                                   AS total,
       count(*) FILTER (WHERE solved_at IS NOT NULL)              AS resolved,
       avg(EXTRACT(EPOCH FROM (taken_at - opened_at))) FILTER (WHERE taken_at IS NOT NULL)    AS mtta_s,
       avg(EXTRACT(EPOCH FROM (solved_at - taken_at))) FILTER (WHERE solved_at IS NOT NULL)  AS mttr_s,
       max(imported_at)                                           AS last_import,
       avg(EXTRACT(EPOCH FROM (solved_at - opened_at))) FILTER (WHERE solved_at IS NOT NULL)  AS cycle_s
FROM kpi_raw;

CREATE OR REPLACE VIEW v_kpi_by_person AS
SELECT p.id   AS person_id,
       p.name,
       count(k.id) FILTER (WHERE k.solved_at IS NOT NULL)                                      AS resolved,
       avg(EXTRACT(EPOCH FROM (k.taken_at - k.opened_at))) FILTER (WHERE k.taken_at IS NOT NULL) AS mtta_s,
       avg(EXTRACT(EPOCH FROM (k.solved_at - k.taken_at))) FILTER (WHERE k.solved_at IS NOT NULL) AS mttr_s,
       avg(EXTRACT(EPOCH FROM (k.solved_at - k.opened_at))) FILTER (WHERE k.solved_at IS NOT NULL) AS cycle_s
FROM persons p
LEFT JOIN kpi_raw k ON k.person_id = p.id
GROUP BY p.id, p.name
ORDER BY p.name;

-- KPI par categorie, synchronises sur la CATEGORIE (arborescence) : chaque categorie de ticket
-- est rapprochee d'une classe (comme v_tickets) ; les colonnes SLA (vert/rouge) s'ajoutent EN FIN
-- (regle CREATE OR REPLACE).
CREATE OR REPLACE VIEW v_kpi_by_category AS
WITH t AS (
  SELECT k.category,
         k.taken_at IS NOT NULL                              AS pris_en_charge,
         k.solved_at IS NOT NULL                             AS resolu,
         EXTRACT(EPOCH FROM (k.taken_at - k.opened_at))      AS mtta_s,
         EXTRACT(EPOCH FROM (k.solved_at - k.taken_at))      AS mttr_s,
         EXTRACT(EPOCH FROM (k.solved_at - k.opened_at))     AS cycle_s,
         c.classe,
         sl.sla_minutes,
         (k.solved_at IS NOT NULL AND sl.sla_minutes IS NOT NULL
          AND EXTRACT(EPOCH FROM (k.solved_at - k.taken_at)) <= sl.sla_minutes * 60) AS sla_ok,
         (sl.sla_minutes IS NOT NULL)                        AS sla_mesure
  FROM kpi_raw k
  LEFT JOIN LATERAL (
    SELECT x.classe FROM categories x
    WHERE cat_norm(x.classe) = cat_norm(k.category) OR lower(x.key) = lower(k.category)
    ORDER BY x.niveau LIMIT 1
  ) c ON true
  LEFT JOIN categories sl ON sl.classe = c.classe AND sl.niveau = k.niveau
)
SELECT category,
       count(*)                                              AS total,
       count(*) FILTER (WHERE resolu)                        AS resolved,
       avg(mtta_s) FILTER (WHERE pris_en_charge)             AS mtta_s,
       avg(mttr_s) FILTER (WHERE resolu)                     AS mttr_s,
       avg(cycle_s) FILTER (WHERE resolu)                    AS cycle_s,
       max(classe)                                           AS classe,
       count(*) FILTER (WHERE sla_mesure)                    AS sla_n,
       count(*) FILTER (WHERE sla_ok)                        AS sla_ok,
       round(100.0 * count(*) FILTER (WHERE sla_ok)
           / nullif(count(*) FILTER (WHERE sla_mesure), 0), 1) AS sla_pct
FROM t
GROUP BY category
ORDER BY total DESC;

CREATE OR REPLACE VIEW v_kpi_monthly AS
SELECT date_trunc('month', solved_at) AS month,
       count(*) AS resolved,
       avg(EXTRACT(EPOCH FROM (taken_at - opened_at))) FILTER (WHERE taken_at IS NOT NULL)   AS mtta_s,
       avg(EXTRACT(EPOCH FROM (solved_at - taken_at))) FILTER (WHERE solved_at IS NOT NULL) AS mttr_s,
       avg(EXTRACT(EPOCH FROM (solved_at - opened_at))) FILTER (WHERE solved_at IS NOT NULL) AS cycle_s
FROM kpi_raw
WHERE solved_at IS NOT NULL
GROUP BY 1
ORDER BY 1 DESC;

-- Etat instantane des tickets importes (ecran KPI, outil MCP list_tickets)
-- Le SLA d'un ticket se lit sur (classe, niveau) : la categorie libre d'un ticket est rapprochee
-- d'une classe de la taxonomie (par cle exacte ou par nom de classe, ligne de niveau minimal),
-- puis le SLA est celui de la ligne portant le niveau du ticket.
CREATE OR REPLACE VIEW v_tickets AS
SELECT k.id, k.source, k.external_id, k.title, k.status, k.category, k.priority,
       k.opened_at, k.taken_at, k.solved_at, k.imported_at, k.person_id, pe.name AS person,
       EXTRACT(EPOCH FROM (k.taken_at - k.opened_at))  AS mtta_s,
       EXTRACT(EPOCH FROM (k.solved_at - k.taken_at)) AS mttr_s,
       EXTRACT(EPOCH FROM (k.solved_at - k.opened_at)) AS cycle_s,
       -- nouvelles colonnes EN FIN (regle CREATE OR REPLACE) : taxonomie et SLA
       k.niveau,
       c.type,
       c.classe,
       sl.sla_minutes,
       -- SLA vivant : resolu = tenu/depasse (MTTR) ; ouvert = deja hors delai (rouge) ou encore
       -- dans les temps (bleu). Sans cette branche, un ticket GLPI non resolu — la majority d'un
       -- board — affichait « — » et le SLA partage avec le support semblait absent.
       CASE WHEN sl.sla_minutes IS NULL THEN NULL
            WHEN k.solved_at IS NOT NULL THEN
                 CASE WHEN EXTRACT(EPOCH FROM (k.solved_at - k.taken_at)) <= sl.sla_minutes * 60
                      THEN 'ok' ELSE 'depasse' END
            WHEN EXTRACT(EPOCH FROM (now() - coalesce(k.taken_at, k.opened_at))) > sl.sla_minutes * 60
                 THEN 'depasse'
            ELSE 'dans_delai' END AS sla_status
FROM kpi_raw k
LEFT JOIN persons pe ON pe.id = k.person_id
LEFT JOIN LATERAL (
  SELECT x.type, x.classe FROM categories x
  WHERE cat_norm(x.classe) = cat_norm(k.category) OR lower(x.key) = lower(k.category)
  ORDER BY x.niveau LIMIT 1
) c ON true
LEFT JOIN categories sl ON sl.type = c.type AND sl.classe = c.classe AND sl.niveau = k.niveau
ORDER BY k.opened_at DESC;

-- Qui est sur quoi (dashboard + outil MCP who_is_on)
CREATE OR REPLACE VIEW v_open_assignments AS
SELECT a.id,
       a.role,
       a.status,
       a.progress,
       a.start_date,
       a.due_date,
       a.project_id,
       p.name            AS project,
       p.due_date       AS project_due_date,
       a.person_id,
       pe.name           AS person,
       (a.due_date IS NOT NULL AND a.due_date < current_date AND a.status <> 'done') AS overdue,
       (SELECT max(n.at) FROM notes n WHERE n.assignment_id = a.id) AS last_note_at
FROM assignments a
JOIN projects p  ON p.id = a.project_id
JOIN persons  pe ON pe.id = a.person_id
WHERE a.status <> 'done'
ORDER BY a.due_date NULLS LAST, pe.name;

CREATE OR REPLACE VIEW v_late_milestones AS
SELECT id, name, status, due_date, (current_date - due_date) AS days_late
FROM projects
WHERE due_date IS NOT NULL AND due_date < current_date AND status NOT IN ('done', 'cancelled')
ORDER BY due_date;

-- Matrice competences x personnes (pivot jsonb, un rendu suffit) : cles = BASES DE CONNAISSANCE,
-- i.e. les classes de la taxonomie (une colonne par classe, niveau max saisi sur ses lignes).
CREATE OR REPLACE VIEW v_skill_matrix AS
SELECT p.id AS person_id,
       p.name,
       coalesce(jsonb_object_agg(c.classe, x.lvl) FILTER (WHERE x.person_id IS NOT NULL),
                '{}'::jsonb) AS levels
FROM persons p
LEFT JOIN (SELECT s.person_id, c.classe, max(s.level) AS lvl
           FROM skills s JOIN categories c ON c.key = s.category
           GROUP BY s.person_id, c.classe) x ON x.person_id = p.id
LEFT JOIN categories c ON c.classe = x.classe AND c.niveau = 'N1'
WHERE p.active
GROUP BY p.id, p.name
ORDER BY p.name;

CREATE OR REPLACE VIEW v_objectives_pct AS
SELECT person_id,
       period,
       count(*) AS n,
       round(avg(least(coalesce(achieved, 0) / nullif(target, 0), 1)) * 100) AS pct
FROM objectives
GROUP BY person_id, period
ORDER BY period DESC, person_id;

-- Interventions support (ecran SUPPORT, outil MCP list_support)
-- La fiche stocke la cle de sa classe (ligne de niveau minimal) + son niveau ; le SLA de la fiche
-- est celui de la ligne taxonomique (classe, niveau). MTTR > SLA => 'depasse' (rouge).
CREATE OR REPLACE VIEW v_support AS
SELECT s.id,
       s.person_id,
       pe.name           AS person,
       s.category,
       c.classe          AS category_label,
       s.title,
       s.reported_at,
       s.resolved_at,
       s.solution,
       (s.resolved_at IS NULL)                                   AS en_cours,
       -- MTTD = delai entre l'ouverture de la fiche et le signalement sur le terrain. Une fiche
       -- saisie apres coup (intervention rattrapee) ne le connait pas : NULL, pas zero.
       CASE WHEN s.reported_at >= s.created_at
            THEN EXTRACT(EPOCH FROM (s.reported_at - s.created_at)) END    AS mttd_s,
       EXTRACT(EPOCH FROM (s.resolved_at - s.reported_at))       AS mttr_s,
       s.created_at,                                          -- en fin de liste : cf. regle ci-dessus
       -- nouvelles colonnes EN FIN (regle CREATE OR REPLACE) : taxonomie et SLA
       s.niveau,
       c.type,
       c.classe,
       sl.sla_minutes,
        -- SLA vivant, comme les tickets : resolu = tenu (vert) / depasse (rouge) ; une fiche ouverte
        -- deja au-dela de son delai est rouge, sinon « dans les temps » (bleu). NULL sans SLA defini.
CASE WHEN sl.sla_minutes IS NULL THEN NULL
             WHEN s.resolved_at IS NOT NULL THEN
                  CASE WHEN EXTRACT(EPOCH FROM (s.resolved_at - s.reported_at)) <= sl.sla_minutes * 60
                       THEN 'ok' ELSE 'depasse' END
             WHEN EXTRACT(EPOCH FROM (now() - s.reported_at)) > sl.sla_minutes * 60
                  THEN 'depasse'
             ELSE 'dans_delai' END                            AS sla_status,
        -- nouvelle colonne EN FIN (regle CREATE OR REPLACE) : statut d'avancement (Kanban)
        s.status
FROM support s
JOIN persons pe  ON pe.id = s.person_id
JOIN categories c ON c.key = s.category
LEFT JOIN categories sl ON sl.type = c.type AND sl.classe = c.classe AND sl.niveau = s.niveau
ORDER BY s.reported_at DESC;

-- Analyse de competence par categorie technique : interventions support + tickets de l'outil de
-- tickets + projets. Une categorie = volume, taux de reussite, MTTD/MTTR, projets dans les delais.
-- La reussite et les delais se mesurent sur les MÊMES intervalles que ci-dessus :
--   support : MTTD = reported_at-created_at, MTTR = resolved_at-reported_at
--   tickets : MTTD = taken_at-opened_at,      MTTR = solved_at-taken_at
-- (pour le ticket, la prise en charge remplace l'ouverture de la fiche support)
-- Analyse par PERSONNE : ce que chaque technicien a reellement traite (interventions support de
-- l'onglet Support + tickets importes de GLPI + affectations de projets), avec lecture sur la
-- taxonomie : chaque tache porte un niveau de difficulte (N1..N3) et un SLA (classe, niveau) dont
-- le resultat est vert/rouge. Reussite et SLA tiennent compte des STATUTS support (une fiche
-- Terminee ou resolue compte traitee ; Bloque — Escalade compte comme non solde, elle n'est
-- jamais un succes) et du SLA des tickets (sla_pct = part des taches closes dans leur delai,
-- toutes sources confondues). Le NIVEAU (N1..N3) est un attribut des TACHES, jamais des personnes.
-- La realisation des projets (part des affectations terminees) est une competences a part entiere.
-- f_person_analysis(debut, fin) filtre les evenements sur une periode (date / semaine / mois /
-- annee) : taches signalees ou resolues dans la fenetre, affectations terminees dans la fenetre.
-- Vue sans filtre = f_person_analysis(NULL, NULL), pour l'API et le MCP.
-- ponytail: grille N1..N3 tenue en base ; la ponderation d'une note globale, si besoin, n'est pas
-- reprise ici (le metier lit l'activite, pas un nombre magique).
DROP VIEW IF EXISTS v_person_analysis;
DROP FUNCTION IF EXISTS f_person_analysis(timestamptz, timestamptz);
CREATE FUNCTION f_person_analysis(p_from timestamptz, p_to timestamptz)
RETURNS TABLE (person_id int, name text, interventions bigint, tickets bigint, total bigint,
               traites bigint, succes_pct numeric, mttd_s numeric, mttr_s numeric,
               sla_pct numeric, sla_ok bigint, sla_n bigint, diff_moy numeric, diff_max integer,
               progression boolean, projets bigint, proj_done bigint, proj_pct numeric,
               escalades bigint, en_cours bigint) LANGUAGE sql STABLE AS $$
WITH taches AS (
  -- chaque tache avec son statut, son MTTR, sa difficulte (N1=1, N2=2, N3=3) et le SLA (classe, niveau)
  SELECT s.person_id,
         (s.resolved_at IS NOT NULL OR s.status = 'done')        AS close,
         (s.status = 'escalade')                                 AS escalade,
         EXTRACT(EPOCH FROM (s.resolved_at - s.reported_at))     AS mttr_s,
         sl.sla_minutes,
         CASE s.niveau WHEN 'N2' THEN 2 WHEN 'N3' THEN 3 ELSE 1 END AS diff
  FROM support s
  JOIN categories c ON c.key = s.category
  LEFT JOIN categories sl ON sl.type = c.type AND sl.classe = c.classe AND sl.niveau = s.niveau
  WHERE (p_from IS NULL OR s.reported_at >= p_from) AND (p_to IS NULL OR s.reported_at <= p_to)
  UNION ALL
  SELECT k.person_id,
         (k.solved_at IS NOT NULL)                           AS close,
         false                                               AS escalade,
         EXTRACT(EPOCH FROM (k.solved_at - k.taken_at))          AS mttr_s,
         sl.sla_minutes,
         CASE k.niveau WHEN 'N2' THEN 2 WHEN 'N3' THEN 3 ELSE 1 END AS diff
  FROM kpi_raw k
  LEFT JOIN LATERAL (
    SELECT x.type, x.classe FROM categories x
    WHERE cat_norm(x.classe) = cat_norm(k.category) OR lower(x.key) = lower(k.category)
    ORDER BY x.niveau LIMIT 1
  ) c ON true
  LEFT JOIN categories sl ON sl.type = c.type AND sl.classe = c.classe AND sl.niveau = k.niveau
  WHERE k.person_id IS NOT NULL
    AND (p_from IS NULL OR k.opened_at >= p_from) AND (p_to IS NULL OR k.opened_at <= p_to)
), sup AS (
  SELECT person_id,
         count(*)                                                   AS interventions,
         count(*) FILTER (WHERE resolved_at IS NOT NULL OR status = 'done') AS resolved,
         count(*) FILTER (WHERE reported_at >= created_at)          AS mttd_n,
         avg(EXTRACT(EPOCH FROM (reported_at - created_at))) FILTER (WHERE reported_at >= created_at) AS mttd_s,
         avg(EXTRACT(EPOCH FROM (resolved_at - reported_at))) FILTER (WHERE resolved_at IS NOT NULL) AS mttr_s
  FROM support
  WHERE (p_from IS NULL OR reported_at >= p_from) AND (p_to IS NULL OR reported_at <= p_to)
  GROUP BY person_id
), tik AS (
  SELECT person_id,
         count(*)                                                AS tickets,
         count(*) FILTER (WHERE solved_at IS NOT NULL)           AS tik_resolved,
         count(*) FILTER (WHERE taken_at IS NOT NULL)            AS tik_mttd_n,
         avg(EXTRACT(EPOCH FROM (taken_at - opened_at))) FILTER (WHERE taken_at IS NOT NULL)     AS tik_mttd_s,
         avg(EXTRACT(EPOCH FROM (solved_at - taken_at))) FILTER (WHERE solved_at IS NOT NULL)     AS tik_mttr_s
  FROM kpi_raw
  WHERE (p_from IS NULL OR opened_at >= p_from) AND (p_to IS NULL OR opened_at <= p_to)
  GROUP BY person_id
), prj AS (
  -- realisation des projets : les affectations de la periode, part de celles terminees
  SELECT a.person_id,
         count(*)                                       AS projets,
         count(*) FILTER (WHERE a.status = 'done')      AS proj_done
  FROM assignments a
  WHERE p_from IS NULL OR a.start_date >= p_from::date OR a.due_date >= p_from::date
        OR a.created_at >= p_from
        OR EXISTS (SELECT 1 FROM notes n WHERE n.assignment_id = a.id AND n.at >= p_from)
  GROUP BY a.person_id
), tch AS (
  -- agrégats par personne sur les taches : SLA tenu (toutes sources), difficulte, escalades, encours
  SELECT person_id,
         count(*) FILTER (WHERE sla_minutes IS NOT NULL AND close
                          AND mttr_s <= sla_minutes * 60)  AS sla_ok,
         count(*) FILTER (WHERE sla_minutes IS NOT NULL AND close) AS sla_n,
         count(*) FILTER (WHERE escalade)                   AS escalades,
         count(*) FILTER (WHERE NOT close)                    AS en_cours,
         avg(diff)                                          AS diff_moy,
         max(diff) FILTER (WHERE close AND mttr_s IS NOT NULL
                           AND (sla_minutes IS NULL OR mttr_s <= sla_minutes * 60)) AS diff_max
  FROM taches GROUP BY person_id
), per AS (
  SELECT p.id AS person_id, p.name,
         coalesce(sup.interventions, 0)                                          AS interventions,
         coalesce(tik.tickets, 0)                                                AS tickets,
         coalesce(sup.interventions, 0) + coalesce(tik.tickets, 0)               AS total,
         coalesce(sup.resolved, 0) + coalesce(tik.tik_resolved, 0)               AS traites,
         -- MTTD/MTTR melanges : moyenne ponderee par le nombre de cas reellement mesures dans
         -- chaque source (le MTTD se lit sur les fiches ouvertes a temps, le MTTR sur les resolues)
         (coalesce(sup.mttd_s * sup.mttd_n, 0) + coalesce(tik.tik_mttd_s * tik.tik_mttd_n, 0))
           / nullif(coalesce(sup.mttd_n, 0) + coalesce(tik.tik_mttd_n, 0), 0)     AS mttd_s,
         (coalesce(sup.mttr_s * sup.resolved, 0) + coalesce(tik.tik_mttr_s * tik.tik_resolved, 0))
           / nullif(coalesce(sup.resolved, 0) + coalesce(tik.tik_resolved, 0), 0) AS mttr_s,
         coalesce(tch.sla_ok, 0)             AS sla_ok,
         coalesce(tch.sla_n, 0)              AS sla_n,
         coalesce(tch.diff_moy, 0)           AS diff_moy,
         coalesce(tch.diff_max, 0)           AS diff_max,
         coalesce(tch.escalades, 0)          AS escalades,
         coalesce(tch.en_cours, 0)           AS en_cours,
         coalesce(prj.projets, 0)            AS projets,
         coalesce(prj.proj_done, 0)          AS proj_done
  FROM persons p
  LEFT JOIN sup ON sup.person_id = p.id
  LEFT JOIN tik ON tik.person_id = p.id
  LEFT JOIN prj ON prj.person_id = p.id
  LEFT JOIN tch ON tch.person_id = p.id
  WHERE p.active
)
SELECT person_id, name, interventions, tickets, total, traites,
       round(100.0 * traites / nullif(total, 0), 1)  AS succes_pct,
       mttd_s,
       mttr_s,
       round(100.0 * sla_ok / nullif(sla_n, 0), 1)   AS sla_pct,
       sla_ok,
       sla_n,
       round(diff_moy::numeric, 2)                   AS diff_moy,
       diff_max,
       (coalesce(diff_max, 0) >= 2)                  AS progression,
       projets,
       proj_done,
       round(100.0 * proj_done / nullif(projets, 0), 1) AS proj_pct,
       escalades,
       en_cours
FROM per
ORDER BY name;
$$;

CREATE VIEW v_person_analysis AS SELECT * FROM f_person_analysis(NULL, NULL);

-- Fiche personne : affectations, historique competences, dernieres notes (1 ligne JSON)
CREATE OR REPLACE VIEW v_person_history AS
SELECT pe.id     AS person_id,
       pe.name,
       coalesce(jsonb_agg(jsonb_build_object('id', a.id, 'project', p.name, 'role', a.role,
                                             'status', a.status, 'progress', a.progress,
                                             'due_date', a.due_date)
                          ORDER BY a.created_at DESC)
                FILTER (WHERE a.id IS NOT NULL), '[]'::jsonb) AS assignments,
coalesce((SELECT jsonb_agg(jsonb_build_object('skill', l.skill, 'level', l.level,
                                                     'previous', l.previous_level, 'note', l.note,
                                                     'at', l.at) ORDER BY l.at DESC)
                  FROM skills_log l WHERE l.person_id = pe.id), '[]'::jsonb) AS skills,
       coalesce((SELECT jsonb_agg(jsonb_build_object('project', p2.name, 'text', n.text,
                                                    'progress', n.progress, 'at', n.at) ORDER BY n.at DESC)
                 FROM notes n
                 JOIN assignments a2 ON a2.id = n.assignment_id
                 JOIN projects p2 ON p2.id = a2.project_id
                 WHERE a2.person_id = pe.id), '[]'::jsonb) AS notes,
        pe.glpi_id,                            -- en fin de liste : cf. regle CREATE OR REPLACE ci-dessus
        coalesce((SELECT jsonb_agg(jsonb_build_object('id', su.id, 'category', su.category,
                                                     'title', su.title, 'reported_at', su.reported_at,
                                                     'resolved_at', su.resolved_at, 'solution', su.solution)
                                   ORDER BY su.reported_at DESC)
                  FROM support su WHERE su.person_id = pe.id), '[]'::jsonb) AS support
FROM persons pe
LEFT JOIN assignments a ON a.person_id = pe.id
LEFT JOIN projects p ON p.id = a.project_id
GROUP BY pe.id, pe.name, pe.glpi_id;

-- ---------------------------------------------------------------- synchronisation dynamique
-- Chaque ecriture sur les tables qui alimentent le suivi des competences (support, tickets,
-- affectations, competences, objectifs, categories/SLA) notifie le canal 'competences' ;
-- l'ecran Dashboard competence ecoute ce canal (SSE /events) et se recharge des qu'une donnee
-- change — ailleurs dans l'app, via l'API ou par MCP. Idempotent (DROP puis CREATE).
CREATE OR REPLACE FUNCTION sirh_notify_change() RETURNS trigger AS $$
BEGIN
  PERFORM pg_notify('competences', TG_TABLE_NAME);
  RETURN NULL;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS competences_sync ON support;
CREATE TRIGGER competences_sync AFTER INSERT OR UPDATE OR DELETE ON support
  EXECUTE FUNCTION sirh_notify_change();
DROP TRIGGER IF EXISTS competences_sync ON kpi_raw;
CREATE TRIGGER competences_sync AFTER INSERT OR UPDATE OR DELETE ON kpi_raw
  EXECUTE FUNCTION sirh_notify_change();
DROP TRIGGER IF EXISTS competences_sync ON assignments;
CREATE TRIGGER competences_sync AFTER INSERT OR UPDATE OR DELETE ON assignments
  EXECUTE FUNCTION sirh_notify_change();
DROP TRIGGER IF EXISTS competences_sync ON projects;
CREATE TRIGGER competences_sync AFTER INSERT OR UPDATE OR DELETE ON projects
  EXECUTE FUNCTION sirh_notify_change();
DROP TRIGGER IF EXISTS competences_sync ON skills;
CREATE TRIGGER competences_sync AFTER INSERT OR UPDATE OR DELETE ON skills
  EXECUTE FUNCTION sirh_notify_change();
DROP TRIGGER IF EXISTS competences_sync ON skills_log;
CREATE TRIGGER competences_sync AFTER INSERT OR UPDATE OR DELETE ON skills_log
  EXECUTE FUNCTION sirh_notify_change();
DROP TRIGGER IF EXISTS competences_sync ON objectives;
CREATE TRIGGER competences_sync AFTER INSERT OR UPDATE OR DELETE ON objectives
  EXECUTE FUNCTION sirh_notify_change();
DROP TRIGGER IF EXISTS competences_sync ON categories;
CREATE TRIGGER competences_sync AFTER INSERT OR UPDATE OR DELETE ON categories
  EXECUTE FUNCTION sirh_notify_change();
-- les personnes alimentent la recherche (datalist) et l'analyse du dashboard : une creation,
-- un renommage ou une desactivation doit aussi recharger l'ecran.
DROP TRIGGER IF EXISTS competences_sync ON persons;
CREATE TRIGGER competences_sync AFTER INSERT OR UPDATE OR DELETE ON persons
  EXECUTE FUNCTION sirh_notify_change();
