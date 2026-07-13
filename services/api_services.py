from . import application as domain


class StateService:
    def get(self):
        return domain.public_state("保存済みの記録を読み込みました。")


class RaffleService:
    def upload(self, filename, content):
        return domain.handle_upload(filename, content)

    def set_roles(self, payload):
        return domain.handle_roles(payload)

    def draw(self, payload):
        return domain.run_raffle(payload)

    def set_mode(self, payload):
        return domain.handle_mode(payload)

    def set_special_rule(self, payload):
        return domain.handle_special(payload)

    def toggle_exclusion(self, payload):
        return domain.handle_exclude(payload)


class HistorySyncService:
    def upload(self, filename, content):
        return domain.handle_history_upload(filename, content)

    def apply(self, payload):
        return domain.handle_history_apply(payload)

    def rollback(self, payload):
        return domain.handle_history_rollback(payload)


class EventService:
    def select(self, payload):
        return domain.handle_event_select(payload)

    def select_user_list(self, payload):
        return domain.handle_user_event(payload)

    def save(self, payload):
        return domain.handle_event_save(payload)

    def delete(self, payload):
        return domain.handle_event_delete(payload)


class SessionService:
    def get(self, payload):
        return domain.handle_session(payload)

    def delete(self, payload):
        return domain.handle_session_delete(payload)


class ExportService:
    def build_event_workbook(self, event_id):
        return domain.build_event_export(event_id)


class ApiService:
    def __init__(self):
        self.state = StateService()
        self.raffle = RaffleService()
        self.history = HistorySyncService()
        self.events = EventService()
        self.sessions = SessionService()
        self.exports = ExportService()

    def dispatch(self, path, payload=None):
        payload = payload or {}
        routes = {
            "/api/state": self.state.get,
            "/api/roles": lambda: self.raffle.set_roles(payload),
            "/api/raffle": lambda: self.raffle.draw(payload),
            "/api/session": lambda: self.sessions.get(payload),
            "/api/session/delete": lambda: self.sessions.delete(payload),
            "/api/event": lambda: self.events.save(payload),
            "/api/event/select": lambda: self.events.select(payload),
            "/api/event/save": lambda: self.events.save(payload),
            "/api/event/delete": lambda: self.events.delete(payload),
            "/api/user-event": lambda: self.events.select_user_list(payload),
            "/api/history/apply": lambda: self.history.apply(payload),
            "/api/history/rollback": lambda: self.history.rollback(payload),
            "/api/mode": lambda: self.raffle.set_mode(payload),
            "/api/special": lambda: self.raffle.set_special_rule(payload),
            "/api/exclude": lambda: self.raffle.toggle_exclusion(payload),
        }
        if path not in routes:
            raise KeyError(path)
        return routes[path]()


api_service = ApiService()
