import SammyKit
import SwiftUI

/// The user's saved playbooks, and the drafts from teaching Sammy, to review and save.
struct SkillsView: View {
    @Environment(AppModel.self) private var app
    @State private var editing: Skill?
    @State private var deleting: Skill?

    var body: some View {
        Page(
            title: "Skills",
            subtitle: "How you like things done, step by step. \(app.sammyName) follows a skill when a task calls for it.",
            items: app.skills,
            emptyIcon: "book",
            emptyTitle: "No skills yet",
            emptyText: "Show \(app.sammyName) how you do something: take over its browser and press Teach. Or ask it to save what it just did."
        ) { skill in
            HStack(spacing: 12) {
                Image(systemName: skill.draft ? "pencil.line" : "book")
                    .accessibilityHidden(true)
                    .font(.system(size: 13))
                    .foregroundStyle(Palette.onSurfaceVariant)
                    .frame(width: 18)
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text(skill.name).font(.system(size: 13, weight: .medium))
                        if skill.draft { Badge(text: "Draft") }
                    }
                    Text(skill.draft ? "Not used until you save it. \(skill.whenToUse)" : skill.whenToUse)
                        .font(.system(size: 12))
                        .foregroundStyle(Palette.onSurfaceVariant)
                        .lineLimit(2)
                }
                Spacer()
                Button(skill.draft ? "Review…" : "Edit…") { editing = skill }
                    .buttonStyle(.sammy(skill.draft ? .primary : .outline, small: true))
                    .accessibilityLabel("\(skill.draft ? "Review" : "Edit") \(skill.name)")
                Button("Delete…") { deleting = skill }
                    .buttonStyle(.sammy(.outline, small: true))
                    .accessibilityLabel("Delete \(skill.name)")
            }
            .contentShape(Rectangle())
            .onTapGesture { editing = skill }
            .accessibilityAction(named: "Edit") { editing = skill }
        }
        .sheet(item: $editing) { SkillEditor(skill: $0) }
        .confirmationDialog("Delete “\(deleting?.name ?? "")”?", isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } })) {
            Button("Delete skill", role: .destructive) { if let deleting { Task { await app.delete(deleting) } } }
        } message: {
            Text("\(app.sammyName) won't follow it in future tasks.")
        }
    }
}

/// A skill's eight parts, to change; a draft is saved for good from here.
private struct SkillEditor: View {
    @Environment(AppModel.self) private var app
    @Environment(\.dismiss) private var dismiss
    let original: Skill
    @State private var skill: Skill
    @State private var saving = false
    @State private var error: String?
    @FocusState private var focus: String?

    init(skill: Skill) {
        original = skill
        _skill = State(initialValue: skill)
    }

    private var complete: Bool {
        [skill.name, skill.whenToUse, skill.steps].allSatisfy { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 4) {
                Text(original.draft ? "Review draft skill" : "Edit skill").font(.system(size: 15, weight: .semibold))
                if original.draft {
                    Text("\(app.sammyName) wrote this from what you showed it. Check it, then save it for \(app.sammyName) to use.")
                        .font(.system(size: 12))
                        .foregroundStyle(Palette.onSurfaceVariant)
                }
            }
            .padding(20)
            Divider().overlay(Palette.outline)
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    line("Name", text: $skill.name, prompt: "Reorder groceries")
                    line("When to use it", text: $skill.whenToUse, prompt: "When I ask to reorder my usual groceries")
                    long("What it needs", text: $skill.inputs, height: 50)
                    long("Steps", text: $skill.steps, height: 140)
                    long("How to check it worked", text: $skill.verify, height: 50)
                    long("What to give back", text: $skill.returns, height: 50)
                    long("Ask first before", text: $skill.approvals, height: 50)
                    long("If something goes wrong", text: $skill.failures, height: 50)
                }
                .padding(20)
            }
            Divider().overlay(Palette.outline)
            VStack(alignment: .leading, spacing: 10) {
                if let error {
                    NoticeBar(notice: .error(error)) { self.error = nil }
                }
                HStack {
                    Spacer()
                    Button("Cancel") { dismiss() }
                        .buttonStyle(.outline)
                        .keyboardShortcut(.cancelAction)
                    Button {
                        Task { await save() }
                    } label: {
                        HStack(spacing: 6) {
                            if saving { ProgressView().controlSize(.mini).tint(Palette.onLink) }
                            Text(original.draft ? "Save skill" : "Save")
                        }
                    }
                    .buttonStyle(.primary)
                    .keyboardShortcut(.defaultAction)
                    .disabled(saving || !complete)
                    .help(complete ? "Save it for \(app.sammyName) to use (↩)" : "A skill needs a name, when to use it, and its steps.")
                }
            }
            .padding(20)
        }
        .frame(width: 560, height: 640)
        .background(Palette.surface)
    }

    private func save() async {
        saving = true
        defer { saving = false }
        var changed = skill
        changed.draft = false
        error = await app.save(changed)
        if error == nil { dismiss() }
    }

    private func line(_ label: String, text: Binding<String>, prompt: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label).font(.system(size: 12, weight: .medium))
            TextField(label, text: text, prompt: Text(prompt))
                .labelsHidden()
                .accessibilityLabel(label)
                .focused($focus, equals: label)
                .field(focused: focus == label)
        }
    }

    private func long(_ label: String, text: Binding<String>, height: CGFloat) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label).font(.system(size: 12, weight: .medium))
            TextEditor(text: text)
                .font(.system(size: 13))
                .scrollContentBackground(.hidden)
                .padding(.horizontal, 5)
                .padding(.vertical, 6)
                .frame(minHeight: height)
                .background(RoundedRectangle(cornerRadius: Metrics.radiusMedium).fill(Palette.container))
                .overlay(
                    RoundedRectangle(cornerRadius: Metrics.radiusMedium)
                        .strokeBorder(focus == label ? Palette.link : Palette.outline, lineWidth: focus == label ? 1.5 : 1)
                )
                .focused($focus, equals: label)
                .accessibilityLabel(label)
        }
    }
}
