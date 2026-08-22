\restrict dbmate

-- Dumped from database version 16.14 (Debian 16.14-1.pgdg12+1)
-- Dumped by pg_dump version 16.14 (Debian 16.14-1.pgdg12+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


--
-- Name: edges_validate_fn(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.edges_validate_fn() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
  v_src_space text; v_src_type text;
  v_dst_space text; v_dst_type text;
BEGIN
  SELECT space_id, type INTO v_src_space, v_src_type FROM nodes WHERE id = NEW.src_id;
  SELECT space_id, type INTO v_dst_space, v_dst_type FROM nodes WHERE id = NEW.dst_id;

  IF v_src_space IS NULL OR v_dst_space IS NULL THEN
    RAISE EXCEPTION 'edge endpoint does not exist' USING ERRCODE = '23514';
  END IF;
  IF v_src_space <> NEW.space_id OR v_dst_space <> NEW.space_id THEN
    RAISE EXCEPTION 'cross-space edge is not permitted' USING ERRCODE = '23514';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM edge_rules er
    WHERE er.space_id = NEW.space_id AND er.type = NEW.type
      AND er.src_type = v_src_type AND er.dst_type = v_dst_type
  ) THEN
    RAISE EXCEPTION 'edge_rules: no rule for type=%, src=%, dst=%',
      NEW.type, v_src_type, v_dst_type USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;


--
-- Name: engram_addenda_text(jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.engram_addenda_text(attrs jsonb) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$
  SELECT COALESCE(string_agg(elem ->> 'body', ' '), '')
  FROM jsonb_array_elements(
    CASE WHEN jsonb_typeof(attrs -> 'addenda') = 'array'
         THEN attrs -> 'addenda' ELSE '[]'::jsonb END) AS elem
$$;


--
-- Name: engram_readable_scopes(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.engram_readable_scopes() RETURNS SETOF text
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
  SELECT s.id FROM scopes s
  WHERE s.space_id = current_setting('engram.space_id', true)
    AND s.archived = false
    AND ( s.visibility IN ('team-read', 'team-write')
          OR s.owner_principal = current_setting('engram.principal', true)
          OR EXISTS (SELECT 1 FROM scope_grants g
                     WHERE g.space_id = s.space_id AND g.scope_id = s.id
                       AND g.principal = current_setting('engram.principal', true)) )
$$;


--
-- Name: engram_validate_attrs(jsonb, jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.engram_validate_attrs(spec jsonb, attrs jsonb) RETURNS text[]
    LANGUAGE plpgsql IMMUTABLE
    AS $_$
DECLARE
  errors text[] := '{}';  req jsonb;  opt jsonb;  cond jsonb;  closed boolean;
  k text;  v jsonb;  rule jsonb;  c jsonb;  d date;
BEGIN
  req    := COALESCE(spec #> '{attrs,required}', '{}'::jsonb);
  opt    := COALESCE(spec #> '{attrs,optional}', '{}'::jsonb);
  cond   := COALESCE(spec #> '{attrs,requires}', '[]'::jsonb);
  closed := COALESCE((spec #>> '{attrs,closed}')::boolean, true);

  -- Phase 1: required presence
  FOR k IN SELECT jsonb_object_keys(req) ORDER BY 1 LOOP
    IF NOT attrs ? k THEN errors := errors || format('attrs.%s is required', k); END IF;
  END LOOP;

  -- Phase 2: conditionals, array order
  FOR c IN SELECT * FROM jsonb_array_elements(cond) LOOP
    IF attrs ? (c #>> '{when,key}')
       AND jsonb_typeof(attrs -> (c #>> '{when,key}')) = 'string'
       AND attrs ->> (c #>> '{when,key}') = c #>> '{when,equals}'
       AND NOT attrs ? (c ->> 'key') THEN
      errors := errors || format('attrs.%s is required when %s=%s',
                 c ->> 'key', c #>> '{when,key}', c #>> '{when,equals}');
    END IF;
  END LOOP;

  -- Phase 3: closed / unknown keys. `addenda` is engine-reserved (never a
  -- pack-declared key -- see migration header) and exempt from this check.
  IF closed THEN
    FOR k IN SELECT jsonb_object_keys(attrs) ORDER BY 1 LOOP
      IF k <> 'addenda' AND NOT (req ? k OR opt ? k) THEN
        errors := errors || format('attrs.%s is not allowed (closed spec)', k);
      END IF;
    END LOOP;
  END IF;

  -- Phase 4: value checks (lexicographic; rule = req->k else opt->k)
  FOR k IN SELECT jsonb_object_keys(attrs) ORDER BY 1 LOOP
    rule := COALESCE(req -> k, opt -> k);
    CONTINUE WHEN rule IS NULL;
    v := attrs -> k;
    IF rule ? 'enum' THEN
      IF jsonb_typeof(v) <> 'string' OR NOT (rule -> 'enum') ? (v #>> '{}') THEN
        errors := errors || format('attrs.%s must be one of %s', k,
          (SELECT string_agg(e #>> '{}', '|') FROM jsonb_array_elements(rule -> 'enum') e));
      END IF;
    ELSE
      CASE rule ->> 'type'
        WHEN 'string' THEN
          IF jsonb_typeof(v) <> 'string' THEN
            errors := errors || format('attrs.%s must be a string', k);
          ELSIF char_length(v #>> '{}') > 2000 THEN
            errors := errors || format('attrs.%s must be at most 2000 characters', k);
          END IF;
        WHEN 'int' THEN
          IF jsonb_typeof(v) <> 'number' THEN
            errors := errors || format('attrs.%s must be a int', k);
          ELSIF (v #>> '{}')::numeric <> trunc((v #>> '{}')::numeric) THEN
            errors := errors || format('attrs.%s must be a int', k);
          END IF;
        WHEN 'number' THEN
          IF jsonb_typeof(v) <> 'number' THEN
            errors := errors || format('attrs.%s must be a number', k);
          END IF;
        WHEN 'bool' THEN
          IF jsonb_typeof(v) <> 'boolean' THEN
            errors := errors || format('attrs.%s must be a bool', k);
          END IF;
        WHEN 'date' THEN
          IF jsonb_typeof(v) <> 'string' OR (v #>> '{}') !~ '^\d{4}-\d{2}-\d{2}$' THEN
            errors := errors || format('attrs.%s must be a date', k);
          ELSE
            BEGIN
              d := (v #>> '{}')::date;
            EXCEPTION WHEN others THEN
              errors := errors || format('attrs.%s must be a valid ISO date', k);
            END;
          END IF;
      END CASE;
    END IF;
  END LOOP;
  RETURN errors;
END $_$;


--
-- Name: engram_writable_scopes(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.engram_writable_scopes() RETURNS SETOF text
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
  SELECT s.id FROM scopes s
  WHERE s.space_id = current_setting('engram.space_id', true)
    AND s.archived = false
    AND ( s.visibility = 'team-write'
          OR s.owner_principal = current_setting('engram.principal', true)
          OR EXISTS (SELECT 1 FROM scope_grants g
                     WHERE g.space_id = s.space_id AND g.scope_id = s.id
                       AND g.principal = current_setting('engram.principal', true)
                       AND g.level = 'write') )
$$;


--
-- Name: nodes_touch_fn(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.nodes_touch_fn() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
  v_canon_space text;
  v_canon_type  text;
BEGIN
  IF TG_OP = 'UPDATE'
     AND NEW.type              =            OLD.type
     AND NEW.scope_id          =            OLD.scope_id
     AND NEW.title             =            OLD.title
     AND NEW.body              =            OLD.body
     AND NEW.attrs             =            OLD.attrs
     AND NEW.status            =            OLD.status
     AND NEW.canonical_id      IS NOT DISTINCT FROM OLD.canonical_id
     AND NEW.source_client     =            OLD.source_client
     AND NEW.author_principal  =            OLD.author_principal
     AND NEW.source_session    IS NOT DISTINCT FROM OLD.source_session
  THEN
    -- recall-only, re-index (embedding/extra_search), or no-op: leave it.
    NEW.updated_at := OLD.updated_at;
  ELSE
    NEW.updated_at := now();            -- INSERT or a real content edit
  END IF;

  IF NEW.canonical_id IS NOT NULL THEN
    SELECT space_id, type INTO v_canon_space, v_canon_type
      FROM nodes WHERE id = NEW.canonical_id;
    IF v_canon_space IS DISTINCT FROM NEW.space_id OR v_canon_type IS DISTINCT FROM NEW.type THEN
      RAISE EXCEPTION 'canonical_id target must be same space and type' USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NEW;
END $$;


--
-- Name: nodes_validate_attrs_fn(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.nodes_validate_attrs_fn() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
  spec jsonb;
  errors text[];
BEGIN
  SELECT attr_spec INTO spec FROM node_types WHERE space_id = NEW.space_id AND name = NEW.type;
  IF spec IS NULL THEN
    RAISE EXCEPTION 'unknown node type % in space %', NEW.type, NEW.space_id USING ERRCODE = '23514';
  END IF;

  errors := engram_validate_attrs(spec, NEW.attrs);
  IF array_length(errors, 1) > 0 THEN
    RAISE EXCEPTION '%', array_to_string(errors, '; ') USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: api_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.api_tokens (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    space_id text NOT NULL,
    principal text NOT NULL,
    client_name text NOT NULL,
    token_hash text NOT NULL,
    role text NOT NULL,
    revoked boolean DEFAULT false NOT NULL,
    last_used_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    no_scope_all boolean DEFAULT false NOT NULL,
    CONSTRAINT api_tokens_role_check CHECK ((role = ANY (ARRAY['readwrite'::text, 'readonly'::text])))
);


--
-- Name: audit_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_log (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    space_id text NOT NULL,
    principal text,
    client_name text,
    action text NOT NULL,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.config (
    space_id text NOT NULL,
    key text NOT NULL,
    value jsonb NOT NULL
);


--
-- Name: dedup_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dedup_log (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    space_id text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    type text NOT NULL,
    node_id uuid,
    candidate_id uuid,
    similarity real,
    band text NOT NULL,
    author_principal text NOT NULL,
    CONSTRAINT dedup_log_band_check CHECK ((band = ANY (ARRAY['merge'::text, 'pending'::text, 'insert'::text, 'merge_linked'::text, 'merge_linked_promoted'::text])))
);

ALTER TABLE ONLY public.dedup_log FORCE ROW LEVEL SECURITY;


--
-- Name: edge_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.edge_rules (
    space_id text NOT NULL,
    type text NOT NULL,
    src_type text NOT NULL,
    dst_type text NOT NULL
);


--
-- Name: edge_types; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.edge_types (
    space_id text NOT NULL,
    name text NOT NULL,
    description text NOT NULL,
    bidirectional boolean DEFAULT false NOT NULL,
    CONSTRAINT edge_types_name_check CHECK ((name ~ '^[a-z][a-z0-9_]{1,40}$'::text))
);


--
-- Name: edges; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.edges (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    space_id text NOT NULL,
    src_id uuid NOT NULL,
    dst_id uuid NOT NULL,
    type text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT edges_check CHECK ((src_id <> dst_id))
);

ALTER TABLE ONLY public.edges FORCE ROW LEVEL SECURITY;


--
-- Name: inbox; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.inbox (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    space_id text NOT NULL,
    scope_id text,
    status text DEFAULT 'pending'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    kind text,
    payload jsonb
);

ALTER TABLE ONLY public.inbox FORCE ROW LEVEL SECURITY;


--
-- Name: metrics_rollup; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.metrics_rollup (
    space_id text NOT NULL,
    principal text NOT NULL,
    metric text NOT NULL,
    bucket_date date NOT NULL,
    count bigint DEFAULT 0 NOT NULL
);

ALTER TABLE ONLY public.metrics_rollup FORCE ROW LEVEL SECURITY;


--
-- Name: node_types; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.node_types (
    space_id text NOT NULL,
    name text NOT NULL,
    description text NOT NULL,
    attr_spec jsonb NOT NULL,
    CONSTRAINT node_types_name_check CHECK ((name ~ '^[a-z][a-z0-9_]{1,40}$'::text))
);


--
-- Name: nodes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.nodes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    space_id text NOT NULL,
    type text NOT NULL,
    scope_id text NOT NULL,
    title text NOT NULL,
    body text NOT NULL,
    attrs jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    canonical_id uuid,
    embedding public.vector(384) NOT NULL,
    embedding_model text NOT NULL,
    source_client text NOT NULL,
    author_principal text NOT NULL,
    source_session text,
    recall_count integer DEFAULT 0 NOT NULL,
    last_recalled_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    extra_search text DEFAULT ''::text NOT NULL,
    search tsvector GENERATED ALWAYS AS ((((setweight(to_tsvector('english'::regconfig, title), 'A'::"char") || setweight(to_tsvector('english'::regconfig, body), 'B'::"char")) || setweight(to_tsvector('english'::regconfig, extra_search), 'C'::"char")) || setweight(to_tsvector('english'::regconfig, public.engram_addenda_text(attrs)), 'D'::"char"))) STORED,
    CONSTRAINT nodes_body_check CHECK (((char_length(body) >= 1) AND (char_length(body) <= 8000))),
    CONSTRAINT nodes_check CHECK (((status = 'merged'::text) = (canonical_id IS NOT NULL))),
    CONSTRAINT nodes_status_check CHECK ((status = ANY (ARRAY['active'::text, 'superseded'::text, 'merged'::text, 'archived'::text]))),
    CONSTRAINT nodes_title_check CHECK (((char_length(title) >= 3) AND (char_length(title) <= 200)))
);

ALTER TABLE ONLY public.nodes FORCE ROW LEVEL SECURITY;


--
-- Name: pending_writes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pending_writes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    space_id text NOT NULL,
    author_principal text NOT NULL,
    payload jsonb NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.pending_writes FORCE ROW LEVEL SECURITY;


--
-- Name: principals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.principals (
    space_id text NOT NULL,
    id text NOT NULL,
    display_name text NOT NULL,
    role text DEFAULT 'member'::text NOT NULL,
    archived boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT principals_id_check CHECK ((id ~ '^[a-z0-9][a-z0-9-]{1,62}$'::text)),
    CONSTRAINT principals_role_check CHECK ((role = ANY (ARRAY['member'::text, 'space_admin'::text])))
);

ALTER TABLE ONLY public.principals FORCE ROW LEVEL SECURITY;


--
-- Name: schema_migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.schema_migrations (
    version character varying NOT NULL
);


--
-- Name: scope_grants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.scope_grants (
    space_id text NOT NULL,
    scope_id text NOT NULL,
    principal text NOT NULL,
    level text NOT NULL,
    CONSTRAINT scope_grants_level_check CHECK ((level = ANY (ARRAY['read'::text, 'write'::text])))
);

ALTER TABLE ONLY public.scope_grants FORCE ROW LEVEL SECURITY;


--
-- Name: scopes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.scopes (
    space_id text NOT NULL,
    id text NOT NULL,
    display_name text NOT NULL,
    owner_principal text,
    visibility text DEFAULT 'private'::text NOT NULL,
    ambient boolean DEFAULT false NOT NULL,
    hints text[] DEFAULT '{}'::text[] NOT NULL,
    archived boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    description text,
    CONSTRAINT scopes_id_check CHECK ((id ~ '^[a-z0-9][a-z0-9-]{1,62}$'::text)),
    CONSTRAINT scopes_visibility_check CHECK ((visibility = ANY (ARRAY['private'::text, 'team-read'::text, 'team-write'::text])))
);

ALTER TABLE ONLY public.scopes FORCE ROW LEVEL SECURITY;


--
-- Name: spaces; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.spaces (
    id text NOT NULL,
    display_name text NOT NULL,
    pack_name text,
    pack_version integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT spaces_id_check CHECK ((id ~ '^[a-z0-9][a-z0-9-]{1,62}$'::text))
);


--
-- Name: api_tokens api_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_tokens
    ADD CONSTRAINT api_tokens_pkey PRIMARY KEY (id);


--
-- Name: api_tokens api_tokens_space_id_principal_client_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_tokens
    ADD CONSTRAINT api_tokens_space_id_principal_client_name_key UNIQUE (space_id, principal, client_name);


--
-- Name: audit_log audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);


--
-- Name: config config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.config
    ADD CONSTRAINT config_pkey PRIMARY KEY (space_id, key);


--
-- Name: dedup_log dedup_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dedup_log
    ADD CONSTRAINT dedup_log_pkey PRIMARY KEY (id);


--
-- Name: edge_rules edge_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.edge_rules
    ADD CONSTRAINT edge_rules_pkey PRIMARY KEY (space_id, type, src_type, dst_type);


--
-- Name: edge_types edge_types_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.edge_types
    ADD CONSTRAINT edge_types_pkey PRIMARY KEY (space_id, name);


--
-- Name: edges edges_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.edges
    ADD CONSTRAINT edges_pkey PRIMARY KEY (id);


--
-- Name: edges edges_src_id_dst_id_type_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.edges
    ADD CONSTRAINT edges_src_id_dst_id_type_key UNIQUE (src_id, dst_id, type);


--
-- Name: inbox inbox_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inbox
    ADD CONSTRAINT inbox_pkey PRIMARY KEY (id);


--
-- Name: metrics_rollup metrics_rollup_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.metrics_rollup
    ADD CONSTRAINT metrics_rollup_pkey PRIMARY KEY (space_id, principal, metric, bucket_date);


--
-- Name: node_types node_types_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.node_types
    ADD CONSTRAINT node_types_pkey PRIMARY KEY (space_id, name);


--
-- Name: nodes nodes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nodes
    ADD CONSTRAINT nodes_pkey PRIMARY KEY (id);


--
-- Name: pending_writes pending_writes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pending_writes
    ADD CONSTRAINT pending_writes_pkey PRIMARY KEY (id);


--
-- Name: principals principals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.principals
    ADD CONSTRAINT principals_pkey PRIMARY KEY (space_id, id);


--
-- Name: schema_migrations schema_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (version);


--
-- Name: scope_grants scope_grants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scope_grants
    ADD CONSTRAINT scope_grants_pkey PRIMARY KEY (space_id, scope_id, principal);


--
-- Name: scopes scopes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scopes
    ADD CONSTRAINT scopes_pkey PRIMARY KEY (space_id, id);


--
-- Name: spaces spaces_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.spaces
    ADD CONSTRAINT spaces_pkey PRIMARY KEY (id);


--
-- Name: edges_dst_type_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX edges_dst_type_idx ON public.edges USING btree (dst_id, type);


--
-- Name: edges_src_type_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX edges_src_type_idx ON public.edges USING btree (src_id, type);


--
-- Name: inbox_pending_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX inbox_pending_idx ON public.inbox USING btree (space_id, status) WHERE (status = 'pending'::text);


--
-- Name: metrics_rollup_trend; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX metrics_rollup_trend ON public.metrics_rollup USING btree (space_id, metric, bucket_date);


--
-- Name: nodes_canonical_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX nodes_canonical_id_idx ON public.nodes USING btree (canonical_id) WHERE (canonical_id IS NOT NULL);


--
-- Name: nodes_embedding_hnsw_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX nodes_embedding_hnsw_idx ON public.nodes USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: nodes_search_gin_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX nodes_search_gin_idx ON public.nodes USING gin (search);


--
-- Name: nodes_space_scope_type_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX nodes_space_scope_type_status_idx ON public.nodes USING btree (space_id, scope_id, type, status);


--
-- Name: edges edges_validate; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER edges_validate BEFORE INSERT OR UPDATE ON public.edges FOR EACH ROW EXECUTE FUNCTION public.edges_validate_fn();


--
-- Name: nodes nodes_touch; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER nodes_touch BEFORE INSERT OR UPDATE ON public.nodes FOR EACH ROW EXECUTE FUNCTION public.nodes_touch_fn();


--
-- Name: nodes nodes_validate_attrs; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER nodes_validate_attrs BEFORE INSERT OR UPDATE ON public.nodes FOR EACH ROW EXECUTE FUNCTION public.nodes_validate_attrs_fn();


--
-- Name: api_tokens api_tokens_space_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_tokens
    ADD CONSTRAINT api_tokens_space_id_fkey FOREIGN KEY (space_id) REFERENCES public.spaces(id);


--
-- Name: api_tokens api_tokens_space_id_principal_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_tokens
    ADD CONSTRAINT api_tokens_space_id_principal_fkey FOREIGN KEY (space_id, principal) REFERENCES public.principals(space_id, id);


--
-- Name: audit_log audit_log_space_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_space_id_fkey FOREIGN KEY (space_id) REFERENCES public.spaces(id);


--
-- Name: config config_space_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.config
    ADD CONSTRAINT config_space_id_fkey FOREIGN KEY (space_id) REFERENCES public.spaces(id);


--
-- Name: dedup_log dedup_log_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dedup_log
    ADD CONSTRAINT dedup_log_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.nodes(id);


--
-- Name: dedup_log dedup_log_node_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dedup_log
    ADD CONSTRAINT dedup_log_node_id_fkey FOREIGN KEY (node_id) REFERENCES public.nodes(id);


--
-- Name: dedup_log dedup_log_space_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dedup_log
    ADD CONSTRAINT dedup_log_space_id_fkey FOREIGN KEY (space_id) REFERENCES public.spaces(id);


--
-- Name: edge_rules edge_rules_space_id_dst_type_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.edge_rules
    ADD CONSTRAINT edge_rules_space_id_dst_type_fkey FOREIGN KEY (space_id, dst_type) REFERENCES public.node_types(space_id, name);


--
-- Name: edge_rules edge_rules_space_id_src_type_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.edge_rules
    ADD CONSTRAINT edge_rules_space_id_src_type_fkey FOREIGN KEY (space_id, src_type) REFERENCES public.node_types(space_id, name);


--
-- Name: edge_rules edge_rules_space_id_type_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.edge_rules
    ADD CONSTRAINT edge_rules_space_id_type_fkey FOREIGN KEY (space_id, type) REFERENCES public.edge_types(space_id, name) ON DELETE CASCADE;


--
-- Name: edge_types edge_types_space_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.edge_types
    ADD CONSTRAINT edge_types_space_id_fkey FOREIGN KEY (space_id) REFERENCES public.spaces(id);


--
-- Name: edges edges_dst_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.edges
    ADD CONSTRAINT edges_dst_id_fkey FOREIGN KEY (dst_id) REFERENCES public.nodes(id) ON DELETE CASCADE;


--
-- Name: edges edges_space_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.edges
    ADD CONSTRAINT edges_space_id_fkey FOREIGN KEY (space_id) REFERENCES public.spaces(id);


--
-- Name: edges edges_space_id_type_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.edges
    ADD CONSTRAINT edges_space_id_type_fkey FOREIGN KEY (space_id, type) REFERENCES public.edge_types(space_id, name);


--
-- Name: edges edges_src_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.edges
    ADD CONSTRAINT edges_src_id_fkey FOREIGN KEY (src_id) REFERENCES public.nodes(id) ON DELETE CASCADE;


--
-- Name: inbox inbox_space_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inbox
    ADD CONSTRAINT inbox_space_id_fkey FOREIGN KEY (space_id) REFERENCES public.spaces(id);


--
-- Name: metrics_rollup metrics_rollup_space_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.metrics_rollup
    ADD CONSTRAINT metrics_rollup_space_id_fkey FOREIGN KEY (space_id) REFERENCES public.spaces(id) ON DELETE CASCADE;


--
-- Name: node_types node_types_space_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.node_types
    ADD CONSTRAINT node_types_space_id_fkey FOREIGN KEY (space_id) REFERENCES public.spaces(id);


--
-- Name: nodes nodes_canonical_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nodes
    ADD CONSTRAINT nodes_canonical_id_fkey FOREIGN KEY (canonical_id) REFERENCES public.nodes(id);


--
-- Name: nodes nodes_space_id_scope_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nodes
    ADD CONSTRAINT nodes_space_id_scope_id_fkey FOREIGN KEY (space_id, scope_id) REFERENCES public.scopes(space_id, id);


--
-- Name: nodes nodes_space_id_type_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nodes
    ADD CONSTRAINT nodes_space_id_type_fkey FOREIGN KEY (space_id, type) REFERENCES public.node_types(space_id, name);


--
-- Name: pending_writes pending_writes_space_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pending_writes
    ADD CONSTRAINT pending_writes_space_id_fkey FOREIGN KEY (space_id) REFERENCES public.spaces(id);


--
-- Name: principals principals_space_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.principals
    ADD CONSTRAINT principals_space_id_fkey FOREIGN KEY (space_id) REFERENCES public.spaces(id);


--
-- Name: scope_grants scope_grants_space_id_principal_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scope_grants
    ADD CONSTRAINT scope_grants_space_id_principal_fkey FOREIGN KEY (space_id, principal) REFERENCES public.principals(space_id, id);


--
-- Name: scope_grants scope_grants_space_id_scope_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scope_grants
    ADD CONSTRAINT scope_grants_space_id_scope_id_fkey FOREIGN KEY (space_id, scope_id) REFERENCES public.scopes(space_id, id);


--
-- Name: scopes scopes_space_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scopes
    ADD CONSTRAINT scopes_space_id_fkey FOREIGN KEY (space_id) REFERENCES public.spaces(id);


--
-- Name: scopes scopes_space_id_owner_principal_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scopes
    ADD CONSTRAINT scopes_space_id_owner_principal_fkey FOREIGN KEY (space_id, owner_principal) REFERENCES public.principals(space_id, id);


--
-- Name: dedup_log; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.dedup_log ENABLE ROW LEVEL SECURITY;

--
-- Name: dedup_log dedup_log_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY dedup_log_write ON public.dedup_log FOR INSERT WITH CHECK ((space_id = current_setting('engram.space_id'::text, true)));


--
-- Name: edges; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.edges ENABLE ROW LEVEL SECURITY;

--
-- Name: edges edges_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY edges_read ON public.edges FOR SELECT USING (((space_id = current_setting('engram.space_id'::text, true)) AND (EXISTS ( SELECT 1
   FROM public.nodes n
  WHERE (n.id = edges.src_id))) AND (EXISTS ( SELECT 1
   FROM public.nodes n
  WHERE (n.id = edges.dst_id)))));


--
-- Name: edges edges_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY edges_write ON public.edges FOR INSERT WITH CHECK (((space_id = current_setting('engram.space_id'::text, true)) AND (EXISTS ( SELECT 1
   FROM public.nodes n
  WHERE (n.id = edges.src_id))) AND (EXISTS ( SELECT 1
   FROM public.nodes n
  WHERE (n.id = edges.dst_id))) AND ((EXISTS ( SELECT 1
   FROM public.nodes n
  WHERE ((n.id = edges.src_id) AND (n.scope_id IN ( SELECT public.engram_writable_scopes() AS engram_writable_scopes))))) OR (EXISTS ( SELECT 1
   FROM public.nodes n
  WHERE ((n.id = edges.dst_id) AND (n.scope_id IN ( SELECT public.engram_writable_scopes() AS engram_writable_scopes))))))));


--
-- Name: inbox; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.inbox ENABLE ROW LEVEL SECURITY;

--
-- Name: inbox inbox_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY inbox_read ON public.inbox FOR SELECT USING (((space_id = current_setting('engram.space_id'::text, true)) AND ((scope_id IS NULL) OR (scope_id IN ( SELECT public.engram_readable_scopes() AS engram_readable_scopes)))));


--
-- Name: inbox inbox_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY inbox_update ON public.inbox FOR UPDATE USING (((space_id = current_setting('engram.space_id'::text, true)) AND ((scope_id IS NULL) OR (scope_id IN ( SELECT public.engram_writable_scopes() AS engram_writable_scopes))))) WITH CHECK (((space_id = current_setting('engram.space_id'::text, true)) AND ((scope_id IS NULL) OR (scope_id IN ( SELECT public.engram_writable_scopes() AS engram_writable_scopes)))));


--
-- Name: inbox inbox_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY inbox_write ON public.inbox FOR INSERT WITH CHECK (((space_id = current_setting('engram.space_id'::text, true)) AND ((scope_id IS NULL) OR (scope_id IN ( SELECT public.engram_writable_scopes() AS engram_writable_scopes)))));


--
-- Name: metrics_rollup; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.metrics_rollup ENABLE ROW LEVEL SECURITY;

--
-- Name: metrics_rollup metrics_rollup_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY metrics_rollup_read ON public.metrics_rollup FOR SELECT USING ((space_id = current_setting('engram.space_id'::text, true)));


--
-- Name: metrics_rollup metrics_rollup_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY metrics_rollup_update ON public.metrics_rollup FOR UPDATE USING (((space_id = current_setting('engram.space_id'::text, true)) AND (principal = current_setting('engram.principal'::text, true)))) WITH CHECK (((space_id = current_setting('engram.space_id'::text, true)) AND (principal = current_setting('engram.principal'::text, true))));


--
-- Name: metrics_rollup metrics_rollup_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY metrics_rollup_write ON public.metrics_rollup FOR INSERT WITH CHECK (((space_id = current_setting('engram.space_id'::text, true)) AND (principal = current_setting('engram.principal'::text, true))));


--
-- Name: nodes; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.nodes ENABLE ROW LEVEL SECURITY;

--
-- Name: nodes nodes_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY nodes_read ON public.nodes FOR SELECT USING (((space_id = current_setting('engram.space_id'::text, true)) AND (scope_id IN ( SELECT public.engram_readable_scopes() AS engram_readable_scopes))));


--
-- Name: nodes nodes_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY nodes_update ON public.nodes FOR UPDATE USING (((space_id = current_setting('engram.space_id'::text, true)) AND (scope_id IN ( SELECT public.engram_readable_scopes() AS engram_readable_scopes)))) WITH CHECK (((space_id = current_setting('engram.space_id'::text, true)) AND (scope_id IN ( SELECT public.engram_writable_scopes() AS engram_writable_scopes))));


--
-- Name: nodes nodes_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY nodes_write ON public.nodes FOR INSERT WITH CHECK (((space_id = current_setting('engram.space_id'::text, true)) AND (scope_id IN ( SELECT public.engram_writable_scopes() AS engram_writable_scopes))));


--
-- Name: pending_writes; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.pending_writes ENABLE ROW LEVEL SECURITY;

--
-- Name: pending_writes pending_writes_delete; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY pending_writes_delete ON public.pending_writes FOR DELETE USING (((space_id = current_setting('engram.space_id'::text, true)) AND (author_principal = current_setting('engram.principal'::text, true))));


--
-- Name: pending_writes pending_writes_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY pending_writes_read ON public.pending_writes FOR SELECT USING (((space_id = current_setting('engram.space_id'::text, true)) AND (author_principal = current_setting('engram.principal'::text, true))));


--
-- Name: pending_writes pending_writes_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY pending_writes_write ON public.pending_writes FOR INSERT WITH CHECK (((space_id = current_setting('engram.space_id'::text, true)) AND (author_principal = current_setting('engram.principal'::text, true))));


--
-- Name: principals; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.principals ENABLE ROW LEVEL SECURITY;

--
-- Name: principals principals_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY principals_read ON public.principals FOR SELECT USING ((space_id = current_setting('engram.space_id'::text, true)));


--
-- Name: principals principals_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY principals_write ON public.principals FOR INSERT WITH CHECK (((space_id = current_setting('engram.space_id'::text, true)) AND (EXISTS ( SELECT 1
   FROM public.principals a
  WHERE ((a.space_id = current_setting('engram.space_id'::text, true)) AND (a.id = current_setting('engram.principal'::text, true)) AND (a.role = 'space_admin'::text))))));


--
-- Name: scope_grants; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.scope_grants ENABLE ROW LEVEL SECURITY;

--
-- Name: scope_grants scope_grants_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY scope_grants_read ON public.scope_grants FOR SELECT USING ((space_id = current_setting('engram.space_id'::text, true)));


--
-- Name: scope_grants scope_grants_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY scope_grants_write ON public.scope_grants FOR INSERT WITH CHECK (((space_id = current_setting('engram.space_id'::text, true)) AND (EXISTS ( SELECT 1
   FROM public.principals a
  WHERE ((a.space_id = current_setting('engram.space_id'::text, true)) AND (a.id = current_setting('engram.principal'::text, true)) AND (a.role = 'space_admin'::text))))));


--
-- Name: scopes; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.scopes ENABLE ROW LEVEL SECURITY;

--
-- Name: scopes scopes_admin_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY scopes_admin_read ON public.scopes FOR SELECT USING (((space_id = current_setting('engram.space_id'::text, true)) AND (EXISTS ( SELECT 1
   FROM public.principals a
  WHERE ((a.space_id = current_setting('engram.space_id'::text, true)) AND (a.id = current_setting('engram.principal'::text, true)) AND (a.role = 'space_admin'::text))))));


--
-- Name: scopes scopes_admin_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY scopes_admin_update ON public.scopes FOR UPDATE USING (((space_id = current_setting('engram.space_id'::text, true)) AND (EXISTS ( SELECT 1
   FROM public.principals a
  WHERE ((a.space_id = current_setting('engram.space_id'::text, true)) AND (a.id = current_setting('engram.principal'::text, true)) AND (a.role = 'space_admin'::text)))))) WITH CHECK (((space_id = current_setting('engram.space_id'::text, true)) AND (EXISTS ( SELECT 1
   FROM public.principals a
  WHERE ((a.space_id = current_setting('engram.space_id'::text, true)) AND (a.id = current_setting('engram.principal'::text, true)) AND (a.role = 'space_admin'::text))))));


--
-- Name: scopes scopes_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY scopes_read ON public.scopes FOR SELECT USING (((space_id = current_setting('engram.space_id'::text, true)) AND (id IN ( SELECT public.engram_readable_scopes() AS engram_readable_scopes))));


--
-- Name: scopes scopes_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY scopes_write ON public.scopes FOR INSERT WITH CHECK (((space_id = current_setting('engram.space_id'::text, true)) AND (owner_principal = current_setting('engram.principal'::text, true))));


--
-- PostgreSQL database dump complete
--

\unrestrict dbmate


--
-- Dbmate schema migrations
--

INSERT INTO public.schema_migrations (version) VALUES
    ('0001'),
    ('0002'),
    ('0003'),
    ('0004'),
    ('0005'),
    ('0006'),
    ('0007'),
    ('0008'),
    ('0009'),
    ('0010'),
    ('0011'),
    ('0012'),
    ('0013'),
    ('0014'),
    ('0015'),
    ('0016'),
    ('0017'),
    ('0018'),
    ('0019'),
    ('0020'),
    ('0021'),
    ('0022'),
    ('0023');
