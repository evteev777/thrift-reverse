include "features.thrift"
namespace java fixture.other
struct Envelope { 1: required features.Item item, 2: optional features.Mode mode = features.Mode.ON }
service Other extends features.Base { features.Choice select(1: Envelope input), }
