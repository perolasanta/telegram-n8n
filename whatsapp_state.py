class WhatsAppState:
    """Mimics aiogram's FSMContext interface, backed by Supabase instead of FSM storage."""
    def __init__(self, supabase, phone_number: str, restaurant_id: str):
        self.supabase = supabase
        self.phone_number = phone_number
        self.restaurant_id = restaurant_id

    async def get_data(self) -> dict:
        row = self.supabase.table("whatsapp_sessions")\
            .select("data").eq("phone_number", self.phone_number)\
            .eq("restaurant_id", self.restaurant_id).execute()
        return row.data[0]["data"] if row.data else {}

    async def update_data(self, **kwargs):
        data = await self.get_data()
        data.update(kwargs)
        self.supabase.table("whatsapp_sessions").upsert({
            "phone_number": self.phone_number,
            "restaurant_id": self.restaurant_id,
            "data": data
        }).execute()
        return data

    async def set_state(self, state: str | None):
        self.supabase.table("whatsapp_sessions").upsert({
            "phone_number": self.phone_number,
            "restaurant_id": self.restaurant_id,
            "state": state
        }).execute()

    async def clear(self):
        self.supabase.table("whatsapp_sessions")\
            .delete().eq("phone_number", self.phone_number)\
            .eq("restaurant_id", self.restaurant_id).execute()