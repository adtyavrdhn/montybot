import Foundation
import Observation

/// JSON settings stay typed, including provider-specific nested defaults we do not edit.
public indirect enum ModelSettingValue: Codable, Equatable, Sendable {
    case string(String), number(Double), bool(Bool), object([String: ModelSettingValue])
    case array([ModelSettingValue]), null

    public init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if value.decodeNil() { self = .null }
        else if let decoded = try? value.decode(Bool.self) { self = .bool(decoded) }
        else if let decoded = try? value.decode(String.self) { self = .string(decoded) }
        else if let decoded = try? value.decode(Double.self) { self = .number(decoded) }
        else if let decoded = try? value.decode([String: ModelSettingValue].self) { self = .object(decoded) }
        else { self = .array(try value.decode([ModelSettingValue].self)) }
    }

    public func encode(to encoder: Encoder) throws {
        var value = encoder.singleValueContainer()
        switch self {
        case .string(let decoded): try value.encode(decoded)
        case .number(let decoded): try value.encode(decoded)
        case .bool(let decoded): try value.encode(decoded)
        case .object(let decoded): try value.encode(decoded)
        case .array(let decoded): try value.encode(decoded)
        case .null: try value.encodeNil()
        }
    }

    public var text: String? {
        if case .string(let text) = self { return text }
        return nil
    }
}

public struct ModelChoice: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let name: String
    public let thinking: [String]
    public let options: [String: [String]]
    public let defaults: [String: ModelSettingValue]

    public var simpleThinking: [String] { ["low", "medium", "high"].filter(thinking.contains) }
}

public struct ModelPreferences: Codable, Equatable, Sendable {
    public let model: String
    public let settings: [String: ModelSettingValue]
    public let models: [ModelChoice]

    public var selected: ModelChoice? { models.first { $0.id == model } }
}

public struct ModelPreferencesUpdate: Codable, Equatable, Sendable {
    public let model: String
    public let settings: [String: ModelSettingValue]
}

/// Shared by Settings and the composer for one API client. Only confirmed saves change the selection.
@MainActor @Observable
public final class ModelPreferencesModel {
    public private(set) var preferences: ModelPreferences?
    public private(set) var loading = false
    public private(set) var saving = false
    public private(set) var error: String?
    private var owner: String?
    private var generation = 0
    private var saveTask: Task<Void, Never>?
    private var saveError: Error?

    public nonisolated init() {}

    public func reset() {
        generation += 1
        saveTask?.cancel()
        saveTask = nil
        preferences = nil
        owner = nil
        loading = false
        saving = false
        error = nil
        saveError = nil
    }

    public func load(using client: APIClient, user: String) async {
        if owner != user { reset(); owner = user }
        guard !loading, !saving else { return }
        loading = true
        error = nil
        let current = generation
        defer { if current == generation { loading = false } }
        do {
            let result = try await client.modelPreferences()
            guard current == generation else { return }
            preferences = result
            saveError = nil
        } catch {
            guard current == generation else { return }
            self.error = error.localizedDescription
        }
    }

    public func selectModel(_ id: String, using client: APIClient) {
        guard let preferences, preferences.model != id,
              preferences.models.contains(where: { $0.id == id }) else { return }
        // Never carry provider-specific overrides into another model. The server applies its defaults.
        save(ModelPreferencesUpdate(model: id, settings: [:]), using: client)
    }

    public func selectOption(_ field: String, value: String?, using client: APIClient) {
        guard let preferences, let choice = preferences.selected else { return }
        let allowed = field == "thinking" ? choice.thinking : choice.options[field] ?? []
        if let value, !allowed.contains(value) { return }
        var settings = preferences.settings
        settings[field] = value.map(ModelSettingValue.string)
        save(ModelPreferencesUpdate(model: preferences.model, settings: settings), using: client)
    }

    public func value(for field: String) -> String {
        (preferences?.settings[field] ?? preferences?.selected?.defaults[field])?.text ?? ""
    }

    private func save(_ update: ModelPreferencesUpdate, using client: APIClient) {
        guard !loading, !saving else { return }
        saving = true
        error = nil
        saveError = nil
        let current = generation
        saveTask = Task {
            defer { if current == generation { saving = false; saveTask = nil } }
            do {
                let result = try await client.setModelPreferences(update)
                guard current == generation else { return }
                preferences = result
            } catch {
                guard current == generation else { return }
                self.error = "Couldn't save model settings. \(error.localizedDescription)"
                saveError = error
            }
        }
    }

    /// Message creation waits for a pending save, including queued messages and retries.
    /// A failed save blocks sending until the user retries or reloads the confirmed preference.
    public func waitForSave() async throws {
        let current = generation
        await saveTask?.value
        try Task.checkCancellation()
        guard current == generation else { throw CancellationError() }
        if let saveError { throw saveError }
    }
}
