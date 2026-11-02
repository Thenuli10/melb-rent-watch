-- Access control. NOT run by the pipeline: run once by hand in the Supabase SQL
-- editor, replacing the password placeholders first. Never commit real passwords.
--
-- reporting_ro: Power BI and Excel, read-only on analytics views
-- pipeline_rw:  GitHub Actions ETL, writes staging and dw

CREATE ROLE reporting_ro LOGIN PASSWORD '<choose-a-strong-password>';
GRANT USAGE ON SCHEMA analytics, dw TO reporting_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO reporting_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics GRANT SELECT ON TABLES TO reporting_ro;
-- Views run with the owner's rights, so reporting_ro needs no direct dw table access.
-- USAGE on dw is only needed if a view's owner differs; drop it if unused.

CREATE ROLE pipeline_rw LOGIN PASSWORD '<choose-a-different-strong-password>';
GRANT USAGE, CREATE ON SCHEMA staging, dw, analytics TO pipeline_rw;
GRANT ALL ON ALL TABLES IN SCHEMA staging, dw TO pipeline_rw;
GRANT ALL ON ALL SEQUENCES IN SCHEMA staging, dw TO pipeline_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA staging, dw GRANT ALL ON TABLES TO pipeline_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA staging, dw GRANT ALL ON SEQUENCES TO pipeline_rw;

-- On Supabase, connect as these roles through the pooler with the user name
-- '<role>.<project-ref>', e.g. reporting_ro.abcdefghijklmnop
