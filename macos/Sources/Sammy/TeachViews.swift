import SammyKit
import SwiftUI

/// Teach in the takeover's bar: asks what the user will show Sammy, then starts recording.
struct TeachButton: View {
    @Environment(AppModel.self) private var app
    let live: LiveSession
    @State private var asking = false
    @State private var goal = ""

    var body: some View {
        Button {
            goal = ""
            asking = true
        } label: {
            Label("Teach", systemImage: "graduationcap")
        }
        .buttonStyle(.sammy(.outline, small: true))
        .disabled(!live.canDrive)
        .help("Show \(app.sammyName) how you do something here; it becomes a draft skill you review.")
        .popover(isPresented: $asking, arrowEdge: .bottom) {
            VStack(alignment: .leading, spacing: 8) {
                Text("What will you show \(app.sammyName)?").font(.system(size: 13, weight: .semibold))
                TextField("What you will show", text: $goal, prompt: Text("Reorder my usual groceries"))
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 280)
                    .onSubmit(start)
                    .onChange(of: goal) { _, new in
                        if new.count > LiveSession.teachGoalLimit { goal = String(new.prefix(LiveSession.teachGoalLimit)) }
                    }
                Text("Then do it once in the browser, and press Stop and save. Passwords and card details are never recorded.")
                    .font(.system(size: 12))
                    .foregroundStyle(Palette.onSurfaceVariant)
                    .frame(width: 280, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
                HStack {
                    Spacer()
                    Button("Cancel") { asking = false }.keyboardShortcut(.cancelAction)
                    Button("Start recording", action: start)
                        .keyboardShortcut(.defaultAction)
                        .disabled(goal.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
            .padding(12)
        }
    }

    private func start() {
        guard !goal.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        live.teach(goal)
        asking = false
    }
}

/// Under the takeover's bar while teaching: recording, writing the draft, then where to find it.
struct TeachingBar: View {
    @Environment(AppModel.self) private var app
    let chat: ChatModel
    let live: LiveSession

    var body: some View {
        HStack(spacing: 10) {
            switch live.teaching {
            case .idle:
                EmptyView()
            case .recording(let goal):
                Circle().fill(Palette.destructive).frame(width: 8, height: 8).accessibilityHidden(true)
                Text("Recording: \(goal)").font(.system(size: 12, weight: .medium)).lineLimit(1).truncationMode(.tail)
                Text("Passwords and card details are never recorded.")
                    .font(.system(size: 12))
                    .foregroundStyle(Palette.onSurfaceVariant)
                    .lineLimit(1)
                Spacer(minLength: 8)
                Button("Stop and save") { live.stopTeaching() }
                    .buttonStyle(.sammy(.primary, small: true))
                    .disabled(!live.canDrive)
                    .help("Stop recording; \(app.sammyName) writes a draft skill from what you did.")
            case .drafting:
                SammyMark(mood: .working, size: 12)
                Text("Writing a draft skill…").font(.system(size: 12, weight: .medium))
                Spacer(minLength: 8)
            case .taught(let name, _):
                Image(systemName: "checkmark.circle").foregroundStyle(Palette.onSurfaceVariant).accessibilityHidden(true)
                Text("Draft skill “\(name)” saved. Review it in Skills.").font(.system(size: 12, weight: .medium)).lineLimit(1)
                Spacer(minLength: 8)
                Button("Open Skills") {
                    chat.leaveLiveView()
                    app.open(.skills)
                }
                .buttonStyle(.sammy(.outline, small: true))
                .help("Close this view and review the draft. \(app.sammyName) keeps waiting until you take over again and hand it back.")
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 6)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.container)
        .accessibilityElement(children: .contain)
        .onChange(of: live.teaching) { _, teaching in
            switch teaching {
            case .recording(let goal): AccessibilityNotification.Announcement("Recording: \(goal)").post()
            case .drafting: AccessibilityNotification.Announcement("Writing a draft skill").post()
            case .taught(let name, _): AccessibilityNotification.Announcement("Draft skill \(name) saved").post()
            case .idle: break
            }
        }
    }
}
