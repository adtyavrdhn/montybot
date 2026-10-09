import Foundation

// The Skills page: the user's saved playbooks, and the drafts from teaching Sammy, to review.

/// A saved playbook: how the user wants something done, step by step. A draft (from teaching Sammy in its browser)
/// is not used until the user reviews and saves it.
public struct Skill: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public var name: String
    public var whenToUse: String
    public var inputs: String
    public var steps: String
    public var verify: String
    public var returns: String
    public var approvals: String
    public var failures: String
    public var draft: Bool

    enum CodingKeys: String, CodingKey {
        case id, name, inputs, steps, verify, returns, approvals, failures, draft
        case whenToUse = "when_to_use"
    }

    public init(
        id: String, name: String, whenToUse: String, inputs: String = "", steps: String, verify: String = "",
        returns: String = "", approvals: String = "", failures: String = "", draft: Bool = false
    ) {
        self.id = id
        self.name = name
        self.whenToUse = whenToUse
        self.inputs = inputs
        self.steps = steps
        self.verify = verify
        self.returns = returns
        self.approvals = approvals
        self.failures = failures
        self.draft = draft
    }

    /// Drafts first, to review, then by name, as the server lists them.
    static func listed(_ skills: [Skill]) -> [Skill] {
        skills.sorted { ($0.draft ? 0 : 1, $0.name.lowercased()) < ($1.draft ? 0 : 1, $1.name.lowercased()) }
    }
}

extension AppModel {
    public func loadSkills() async { skills = await library { try await self.client.skills() } ?? skills }

    /// Saves the user's changes to a skill (a draft is saved for good with `draft` false). Nil once saved;
    /// otherwise why not, for the form to say: a name already taken, a field left empty.
    public func save(_ skill: Skill) async -> String? {
        await telemetry.action("save skill", ["sammy.skill_id": .string(skill.id), "sammy.skill.draft": .bool(skill.draft)]) { _ in
            do {
                let saved = try await client.updateSkill(skill)
                skills = Skill.listed((skills ?? []).filter { $0.id != saved.id } + [saved])
                return nil
            } catch APIError.signedOut {
                sessionEnded()
                return nil
            } catch APIError.server(status: 404, _) {
                skills?.removeAll { $0.id == skill.id }
                return "This skill was deleted meanwhile."
            } catch APIError.server(status: 422, _) {
                return "A skill needs a name, when to use it, and its steps."
            } catch {
                return error.localizedDescription
            }
        }
    }

    public func delete(_ skill: Skill) async {
        let deleted = await telemetry.action("delete skill", ["sammy.skill_id": .string(skill.id)]) { _ in
            await library({ try await self.client.deleteSkill(skill.id) }) != nil
        }
        if deleted {
            skills?.removeAll { $0.id == skill.id }
        }
    }
}
