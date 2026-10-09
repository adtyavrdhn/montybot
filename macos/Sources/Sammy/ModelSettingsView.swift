import SammyKit
import SwiftUI

struct ModelSettingsView: View {
    @Environment(AppModel.self) private var app

    var body: some View {
        Form {
            if let user = app.user {
                ModelControls(client: app.client, user: user.id)
                Text("Changes are saved to your account and used for the next task. Tasks already running keep their model.")
                    .font(.system(size: 12)).foregroundStyle(.secondary)
            } else {
                Text("Sign in to choose a model.")
            }
        }
        .formStyle(.grouped)
        .frame(height: 360)
    }
}

/// The same controls in Settings and a compact composer popover.
struct ModelControls: View {
    let client: APIClient
    let user: String
    @State private var advanced = false
    private var selection: ModelPreferencesModel { client.modelSelection }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let preferences = selection.preferences {
                if preferences.models.isEmpty {
                    Text("No models are available. Ask your server operator to enable one.")
                } else {
                    Picker("Model", selection: Binding(get: { preferences.model }, set: { selection.selectModel($0, using: client) })) {
                        if preferences.selected == nil { Text(preferences.model).tag(preferences.model) }
                        ForEach(preferences.models) { choice in Text(choice.name).tag(choice.id) }
                    }
                    if let choice = preferences.selected {
                        if !choice.simpleThinking.isEmpty {
                            option("thinking", title: "Thinking", values: choice.simpleThinking)
                        }
                        DisclosureGroup("Advanced", isExpanded: $advanced) {
                            VStack(alignment: .leading, spacing: 10) {
                                ForEach(choice.options.keys.sorted(), id: \.self) { field in
                                    if field != "thinking", let values = choice.options[field], !values.isEmpty {
                                        option(field, title: field.replacingOccurrences(of: "_", with: " ").capitalized, values: values)
                                    }
                                }
                                if choice.options.values.allSatisfy(\.isEmpty) {
                                    Text("No advanced options for this model.").foregroundStyle(.secondary)
                                }
                            }
                            .padding(.top, 8)
                        }
                    }
                }
            }
            if selection.loading || selection.saving {
                ProgressView(selection.saving ? "Saving model settings…" : "Loading models…").controlSize(.small)
            }
            if let error = selection.error {
                Text(error).font(.system(size: 12)).foregroundStyle(Palette.onErrorContainer)
                Button("Reload saved settings") { Task { await selection.load(using: client, user: user) } }
            }
        }
        .disabled(selection.loading || selection.saving)
        .task(id: user) { await selection.load(using: client, user: user) }
    }

    private func option(_ field: String, title: String, values: [String]) -> some View {
        let current = selection.value(for: field)
        return Picker(title, selection: Binding(
            get: { values.contains(current) ? current : "" },
            set: { selection.selectOption(field, value: $0.isEmpty ? nil : $0, using: client) }
        )) {
            Text(current.isEmpty || values.contains(current) ? "Default" : "Default (\(current))").tag("")
            ForEach(values, id: \.self) { Text($0.capitalized).tag($0) }
        }
    }
}

struct ComposerModelPicker: View {
    @Environment(AppModel.self) private var app
    @State private var showing = false

    var body: some View {
        if let user = app.user {
            let selection = app.client.modelSelection
            HStack(spacing: 8) {
                Button { showing.toggle() } label: {
                    Label(selection.preferences?.selected?.name ?? "Choose model", systemImage: "slider.horizontal.3")
                        .lineLimit(1)
                }
                .buttonStyle(.sammy(.ghost, small: true))
                .accessibilityLabel("Model for next task")
                .help("Choose the model and thinking level for your next task")
                .popover(isPresented: $showing) {
                    ScrollView {
                        ModelControls(client: app.client, user: user.id).padding(16)
                    }
                    .frame(width: 340, height: 300)
                }
                if selection.saving || selection.loading { ProgressView().controlSize(.mini) }
                if selection.error != nil {
                    Button("Model settings need attention") { showing = true }
                        .font(.system(size: 11)).foregroundStyle(Palette.onErrorContainer)
                } else if !selection.value(for: "thinking").isEmpty {
                    Text("Thinking: \(selection.value(for: "thinking"))").font(.system(size: 11)).foregroundStyle(.secondary)
                }
            }
            .task(id: user.id) { await selection.load(using: app.client, user: user.id) }
        }
    }
}
