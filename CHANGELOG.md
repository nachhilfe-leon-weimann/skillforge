# Changelog

## [0.6.0](https://github.com/nachhilfe-leon-weimann/skillforge/compare/v0.5.0...v0.6.0) (2026-10-01)


### Features

* **api:** remove the bot API ([#172](https://github.com/nachhilfe-leon-weimann/skillforge/issues/172)) ([7108790](https://github.com/nachhilfe-leon-weimann/skillforge/commit/710879030af85de2ae9b1a5223e8083f1a51d971))
* **auth:** exchange a Discord user for a person token ([#175](https://github.com/nachhilfe-leon-weimann/skillforge/issues/175)) ([f4f731d](https://github.com/nachhilfe-leon-weimann/skillforge/commit/f4f731d8b8728d8d0e194e323caaea7c2e71b545))
* **auth:** link a Discord account with a one-time code ([#177](https://github.com/nachhilfe-leon-weimann/skillforge/issues/177)) ([8670512](https://github.com/nachhilfe-leon-weimann/skillforge/commit/867051213e0d37da8b7632f39520c7a654615d3d))
* **auth:** manage Discord links as identities ([#168](https://github.com/nachhilfe-leon-weimann/skillforge/issues/168)) ([e8e3fa8](https://github.com/nachhilfe-leon-weimann/skillforge/commit/e8e3fa858e8567027cb35938d6fcd4f2ce86eaf0))
* **crm:** signal changes for pulling frontends ([#174](https://github.com/nachhilfe-leon-weimann/skillforge/issues/174)) ([cd8f19a](https://github.com/nachhilfe-leon-weimann/skillforge/commit/cd8f19ac5837cc6b280867b16a9049a462b5a6ce))
* **db:** drop the bot schema and retire the bot scopes ([#173](https://github.com/nachhilfe-leon-weimann/skillforge/issues/173)) ([b5e85e7](https://github.com/nachhilfe-leon-weimann/skillforge/commit/b5e85e7a574bb9923348e2ae1a5beddd75ee0a64))
* **workers:** delete expired sessions and one-time tokens in a housekeeping worker ([#170](https://github.com/nachhilfe-leon-weimann/skillforge/issues/170)) ([889b2c5](https://github.com/nachhilfe-leon-weimann/skillforge/commit/889b2c54ec30398e488e4fd5d8f38f1f85eb9e6f))

## [0.5.0](https://github.com/Nachhilfe-Leon-Weimann/skillforge/compare/v0.4.0...v0.5.0) (2026-09-25)


### Features

* **auth:** add grant modes and the account tables ([#150](https://github.com/Nachhilfe-Leon-Weimann/skillforge/issues/150)) ([e32a541](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/e32a5410b1222e290b26511384d56ca243714057))
* **auth:** add the reach-qualified scope model ([#149](https://github.com/Nachhilfe-Leon-Weimann/skillforge/issues/149)) ([f83c47f](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/f83c47f8cb59193a76b13d2865f339e636422550))
* **auth:** grant client scopes per mode ([#152](https://github.com/Nachhilfe-Leon-Weimann/skillforge/issues/152)) ([8e1f932](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/8e1f93268f820052660efce5428d3c3f6be4cc14))
* **auth:** issue and validate person tokens ([#153](https://github.com/Nachhilfe-Leon-Weimann/skillforge/issues/153)) ([27c2fa3](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/27c2fa3ece6052fe7bc7cd7505917a94b941dc94))
* **auth:** log people in with password and refresh tokens ([#156](https://github.com/Nachhilfe-Leon-Weimann/skillforge/issues/156)) ([b0203a7](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/b0203a71fa5128875dd4604915d8ab9e0f6853b3))
* **auth:** manage user accounts ([#154](https://github.com/Nachhilfe-Leon-Weimann/skillforge/issues/154)) ([712c237](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/712c23773e47cbb2f3098879e12a5c8242abf9b7))
* **crm:** restrict party reads to the caller's reach ([#155](https://github.com/Nachhilfe-Leon-Weimann/skillforge/issues/155)) ([23ed17a](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/23ed17a821a3932f9bb178d93272dc3084af0fa9))

## [0.4.0](https://github.com/Nachhilfe-Leon-Weimann/skillforge/compare/v0.3.0...v0.4.0) (2026-09-20)


### Features

* **api:** add ApiModel and describe the error envelope fields ([90f4980](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/90f49809ef23f655724b5c9fdccc6c3019d3d47b))
* **api:** add the function-scoped DBSession dependency ([b41adb3](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/b41adb3b184442463b261405d3591f0b2e94cdc9))
* **api:** add the pagination vocabulary ([b15d719](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/b15d719e8aadb65dbb0cc495a67766a6f16cdefe))
* **api:** answer unexpected exceptions with the 500 envelope ([9b04e46](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/9b04e46c6f26e8b1179286876a62e9b7bbf763ac))
* **api:** declare API-level errors once with ApiError ([24de9c2](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/24de9c26aa16499e92e330d191a647cf74f1e022))
* **api:** derive 401/403 docs from each operation's security requirement ([69cbcb8](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/69cbcb8800acd3fd2e1b4c7d2e719e5ddc427173))
* **api:** derive operation IDs from domain tag and function name ([ef25453](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/ef2545360dfe153390e8dd8c8790d494fc4cc549))
* **api:** document domain errors and one 422 shape from the status table ([55778d7](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/55778d72e1f949ab7bb9f4e0e44289fddc1a4a5c))
* **api:** emit one error envelope from global exception handlers ([7e00c19](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/7e00c1927e9c8a5c77ad065c6491989691974bcc))
* **api:** keep the validation example on a route-declared 422 ([8baac6e](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/8baac6e83b8a80797a1d1a9c983af937d5dc45b1))
* **auth:** add GET /auth/me ([1bbf9a4](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/1bbf9a4fa6e014462d8829e952025a1f81c50b50))
* **auth:** carry scope descriptions on the Scope enum ([502b093](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/502b093e42cbca7c483b816bb3cc5e2cf749e429))
* **auth:** give the token endpoint's errors the envelope and OAuth2 codes ([315cfeb](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/315cfeb88a2781eb037e03ea88869c1a744fee74))
* **auth:** page the application-client list ([fdfceb4](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/fdfceb4768cf7d1d2ebd6b732cd7e05de56c4538))
* **bot:** validate a student activation against TUTOR_OF ([e3b4b0d](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/e3b4b0d020bbcbdef1db3ce17abe97abd077082d))
* **core:** add the HTTP-agnostic domain error taxonomy ([b06cd2e](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/b06cd2e4a8a4135c88ebee30fb3b4973c3ff50a0))
* **crm:** add party list, search and the guarded delete ([8c436fa](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/8c436fab49f3520b40472688d99bb236e3775477))
* **crm:** add party relations and the reference flow ([1a0b1dc](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/1a0b1dc931eeddc8769c7e7dc0262da10a920e2b))
* **crm:** add the contact info routes ([4303b2a](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/4303b2adc603268588711872681afc9a11a13dde))
* **crm:** add the CRM foundation and the subject routes ([0fb0371](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/0fb0371e2e84d3a012bfbf66d0ef75e9bfa363b8))
* **crm:** add the party aggregate - create, read, update ([ad46b99](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/ad46b99ef73207522b49a32deb0f62ba9e0ab52c))
* **crm:** add the student and tutor roles ([bd2f334](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/bd2f334108570b472965c5915d47c7b0d543b4f8))
* **crm:** filter GET /parties by updated_since ([c710afb](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/c710afb14422a1ff26ec84ded92345685b63e395))
* **crm:** store phone numbers in E.164 ([bad45b1](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/bad45b1dd139c4f0a52e44d51fa09bef301dec26))
* **db:** make subject titles unique regardless of case ([1467ba7](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/1467ba7e29b330f8d8a174b93667cf5ee74f7e30))
* **system:** report the running version on /health ([#115](https://github.com/Nachhilfe-Leon-Weimann/skillforge/issues/115)) ([66a2f26](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/66a2f2625e329a221a0d86828482f361c0717d52))


### Bug Fixes

* **api:** bound the page offset so an oversized value is a 422 ([cbc64f0](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/cbc64f0ff4c07dbb9adac64c5a0cc66533ffddd2))
* **api:** fail at import for an untagged router, keep schema invalidation ([6a4bc5e](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/6a4bc5e0bd762eb682e35d4bb5cfe45029da727f))
* **api:** keep a guarded route's own 401/403 docs next to the derived ones ([c60ea1f](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/c60ea1fbe94d509df4f4c47f607d7f20eda2f625))
* **api:** make ApiError hashable when it carries headers ([9aa5488](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/9aa5488ff363496232c5b63e9c6e67f30418cc49))
* **api:** stamp the 500 envelope with the request id ([fe0ca75](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/fe0ca75371857c693e5dad0cf7b13225c1065a68))
* **bot:** a party's Discord accounts are a collection ([b06c3b0](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/b06c3b037ccb59f67d584e05339ce745c11cc1f2))
* **bot:** document the validation 422 of the stash and pop commits ([d342113](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/d3421135d50627e1cf80cfa2bf6c5ff2167ebf84))
* **crm:** make a subject title conflict fail inside its savepoint ([5258e9c](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/5258e9c64bf099dbf51e8290b9ef518a815d3a40))
* **crm:** make contact normalization a fixed point and bound the open inputs ([f24a458](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/f24a45815032b3d1ebc100de287b7f89ed9f9047))
* **crm:** reject text Postgres cannot store as a validation error ([cce9897](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/cce98970dcd0e82a22fa7d9d09eb641053ba0460))
* **crm:** serialize role writes per person ([9dbdeec](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/9dbdeec255d8107b67ccdd8026e02338c6d7ef0d))
* **db:** hide bound parameters in database errors ([796fc7f](https://github.com/Nachhilfe-Leon-Weimann/skillforge/commit/796fc7f61a662dfce061e39e746f84c26fa8b9c9))
