from datetime import date, datetime, timedelta, timezone

from vitalis.adapters.persistence import HealthRepository, session_scope


def test_naps_without_main_sleep_are_versioned_and_invalidate():
    user = "nap-storage-synthetic"
    day = date(2026,10,7)
    first = datetime(2026,10,7,6,tzinfo=timezone.utc)
    nap = {"started_at":"2026-10-07T05:00:00Z","ended_at":"2026-10-07T05:20:00Z",
           "sleep_minutes":20,"awake_minutes":0,"stages":[],"source":"zepp"}
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user)
        db.flush()
        before = repo.analysis_input_revision(user)
        assert repo.save_naps(user,day,[nap],fetched_at=first)==1
        assert repo.save_naps(user,day,[nap],fetched_at=first+timedelta(minutes=1))==0
        assert repo.analysis_input_revision(user)>before
        assert repo.sleep_range(user,day,day)==[]
        assert repo.nap_range(user,day,day,first-timedelta(seconds=1))=={}
        assert repo.nap_range(user,day,day,first)[day]==[nap]
        assert repo.save_naps(user,day,[],fetched_at=first+timedelta(hours=1))==1
        assert repo.nap_range(user,day,day,first)[day]==[nap]
        assert repo.nap_range(user,day,day,first+timedelta(hours=2))[day]==[]
        assert repo.nap_range("someone-else",day,day,first)=={}
