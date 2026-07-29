// =============================================================================
// app-mongo: demo application database
//
// Run automatically by the mongo image's docker-entrypoint-initdb.d hook the
// first time the volume is created. To re-seed from scratch:
//     docker compose down -v && docker compose up
//
// The `events` collection is mirrored by
// fides-config/resources/app_mongo_dataset.yml.
// =============================================================================

// The entrypoint runs this against MONGO_INITDB_DATABASE, but be explicit so the
// script also works when pasted into `mongosh` by hand. `process` is guarded
// because it is not guaranteed to exist in every mongosh build — an unguarded
// reference throws a ReferenceError and takes the whole seed down.
db = db.getSiblingDB(
  (typeof process !== "undefined" && process.env && process.env.MONGO_INITDB_DATABASE) ||
    "appdb"
);

db.events.drop();

// -----------------------------------------------------------------------------
// The data subject under test — SAME email as in app-postgres.
// -----------------------------------------------------------------------------
db.events.insertMany([
  {
    email: "demo@example.com",
    event_type: "login",
    metadata: {
      ip_address: "203.0.113.42",
      user_agent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
      session_id: "sess_8fa31c",
    },
    timestamp: new Date("2025-02-02T13:58:00Z"),
  },
  {
    email: "demo@example.com",
    event_type: "page_view",
    metadata: {
      ip_address: "203.0.113.42",
      user_agent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
      session_id: "sess_8fa31c",
    },
    timestamp: new Date("2025-02-02T14:01:00Z"),
  },
  {
    email: "demo@example.com",
    event_type: "checkout",
    metadata: {
      ip_address: "198.51.100.7",
      user_agent: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X)",
      session_id: "sess_11b902",
    },
    timestamp: new Date("2025-03-11T10:20:00Z"),
  },
  {
    email: "demo@example.com",
    event_type: "support_ticket",
    metadata: {
      ip_address: "198.51.100.7",
      user_agent: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X)",
      session_id: "sess_11b902",
    },
    timestamp: new Date("2025-04-27T18:50:00Z"),
  },
]);

// -----------------------------------------------------------------------------
// Control subject — must survive any DSAR aimed at demo@example.com.
// -----------------------------------------------------------------------------
db.events.insertMany([
  {
    email: "control@example.com",
    event_type: "login",
    metadata: {
      ip_address: "192.0.2.55",
      user_agent: "Mozilla/5.0 (X11; Linux x86_64)",
      session_id: "sess_c0ffee",
    },
    timestamp: new Date("2025-02-14T07:55:00Z"),
  },
]);

db.events.createIndex({ email: 1 });

print(
  "[app-mongo seed] events seeded: " +
    db.events.countDocuments({}) +
    " total, " +
    db.events.countDocuments({ email: "demo@example.com" }) +
    " for demo@example.com"
);
