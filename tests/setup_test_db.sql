-- Rol y base de mantenimiento para la suite de tests. Se corre UNA vez, como
-- superusuario, contra un Postgres LOCAL (o el del servicio de CI):
--
--   psql -h localhost -U postgres -f tests/setup_test_db.sql
--   docker compose exec -T db psql -U postgres < tests/setup_test_db.sql
--
-- NUNCA contra Neon. Los tests se conectan con este rol, no con el superusuario.
--
-- Permisos RESTRINGIDOS a proposito: puede crear bases (cada corrida arma las
-- suyas, `*_test`, y las borra al terminar) pero no es superusuario, no crea
-- roles, no replica y no saltea RLS. Una base que no es suya no la puede borrar.
-- `tests/conftest.py` verifica estos atributos al arrancar y aborta si el rol
-- tiene mas permisos de los que figuran aca.
--
-- La clave es de un rol local de tests, no un secreto.

CREATE ROLE corpolab_test LOGIN PASSWORD 'corpolab_test'
  NOSUPERUSER NOCREATEROLE CREATEDB NOREPLICATION NOBYPASSRLS;

-- Base de mantenimiento: los tests solo se conectan aca para crear y borrar las
-- bases efimeras de cada corrida. No guarda datos.
CREATE DATABASE corpolab_test OWNER corpolab_test;
