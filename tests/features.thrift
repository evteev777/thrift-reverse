namespace java fixture.core

enum Mode { OFF = 0, ON = 2, NEGATIVE = -4 }
exception Failure { 1: required string message }
struct Item {
  1: required i64 id,
  2: optional string text = "a // { } \" quote",
  3: optional binary blob,
  4: optional map<string, list<set<i32>>> nested,
  5: optional Mode mode = Mode.ON,
  6: optional bool enabled = true,
  7: optional double ratio = -1.25e-3,
  8: optional byte flag = -7,
  9: optional uuid uuidValue,
  10: optional Item next,
}
union Choice { 1: string text, 2: Item item }
struct Empty {}
service Base {
  Item find(3: i64 id) throws (2: Failure problem),
  oneway void notify(1: string message),
  void ping(),
}
service Child extends Base { list<Item> all(1: optional Mode mode), }
