-- State on price observations.
--
-- Woolworths and Coles publish one online price per *state*, not per store, and
-- the scrapes never told the API which store they wanted. The rows are
-- chain-scoped (store_id is null), so the read API attributed them to every
-- store of the chain -- including Perth stores, for a price that may have been
-- the NSW one. Carry the state the price belongs to and match chain rows to
-- stores on (chain_id, state).
--
-- null means "not state-specific": a chain-scope row written by a scrape that
-- could not pin a store. Those keep the old behaviour (matched to every store of
-- the chain) so nothing disappears from the API, but a row that *is* pinned is
-- never attributed outside its own state.

alter table observations add column if not exists state varchar(8);

-- Existing chain-scope rows all came from this Perth deployment's scrapes, so
-- they are treated as WA. Anything a pinned scrape writes from now on carries
-- its own state and is left alone.
update observations
   set state = 'WA'
 where store_id is null
   and state is null;
