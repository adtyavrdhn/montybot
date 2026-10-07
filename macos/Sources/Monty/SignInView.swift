import MontyKit
import SwiftUI

struct RootView: View {
    @Environment(AppModel.self) private var app

    var body: some View {
        Group {
            switch app.phase {
            case .launching:
                Logomark(size: 40).frame(maxWidth: .infinity, maxHeight: .infinity)
                    .accessibilityElement()
                    .accessibilityLabel("Opening Monty")
            case .unreachable:
                EmptyState(
                    icon: "wifi.exclamationmark",
                    title: "Can't reach Monty",
                    text: "Monty's server at \(app.serverURL.host() ?? app.serverURL.absoluteString) isn't answering. You're still signed in; Monty keeps trying."
                ) {
                    Button("Try now") { app.retryNow() }.buttonStyle(.outline)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .signedOut:
                SignInView()
            case .signedIn:
                MainView()
            }
        }
        .background(Palette.surface)
        .tint(Palette.link)
    }
}

/// Sign in or create an account: one card, two fields, one button. The server is tucked away for the few who
/// need another.
struct SignInView: View {
    @Environment(AppModel.self) private var app
    @State private var email = ""
    @State private var password = ""
    /// First time on this Mac: start with creating an account, not "welcome back".
    @State private var creating = !UserDefaults.standard.bool(forKey: "hasSignedIn")
    @State private var working = false
    @State private var error: String?
    @FocusState private var focus: Field?
    /// Resetting a forgotten password: asked for a code, then entering it with a new password.
    @State private var resetting: Reset?
    @State private var code = ""

    enum Field { case email, password, code }
    enum Reset { case asking, entering }

    var body: some View {
        VStack(spacing: 0) {
            Spacer(minLength: 40)
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: 10) {
                    Logomark(size: 26)
                    Text("Monty").font(.system(size: 17, weight: .semibold))
                }
                .padding(.bottom, 22)

                Text(resetting != nil ? "Reset your password" : creating ? "Create your account" : "Sign in to Monty")
                    .font(.system(size: 20, weight: .semibold))
                    .accessibilityAddTraits(.isHeader)
                if let resetting {
                    Text(resetting == .asking ? "We'll email you a code." : "Enter the code we emailed to \(email), and a new password.")
                        .font(.system(size: 13))
                        .foregroundStyle(Palette.onSurfaceVariant)
                        .padding(.top, 4)
                        .padding(.bottom, 18)
                } else if creating {
                    VStack(alignment: .leading, spacing: 6) {
                        promise("globe", "Monty does tasks on the web for you, in its own browser.")
                        promise("hand.raised", "It asks before it buys or sends anything.")
                        promise("key", "You sign in to sites yourself; Monty never sees your passwords.")
                    }
                    .padding(.top, 10)
                    .padding(.bottom, 20)
                } else {
                    Spacer().frame(height: 18)
                }

                label("Email")
                TextField("Email", text: $email, prompt: Text("you@example.com"))
                    .labelsHidden()
                    .accessibilityLabel("Email")
                    .textContentType(.username)
                    .focused($focus, equals: .email)
                    .field(focused: focus == .email)
                    .onSubmit { focus = .password }
                    .padding(.bottom, 12)

                if resetting == .entering {
                    label("Code")
                    TextField("Code", text: $code, prompt: Text("6 digits"))
                        .labelsHidden()
                        .accessibilityLabel("Code")
                        .textContentType(.oneTimeCode)
                        .focused($focus, equals: .code)
                        .field(focused: focus == .code)
                        .onSubmit { focus = .password }
                        .padding(.bottom, 12)
                }
                if resetting != .asking {
                label(resetting == .entering ? "New password" : "Password")
                SecureField("Password", text: $password, prompt: Text(creating ? "At least 8 characters" : ""))
                    .labelsHidden()
                    .accessibilityLabel("Password")
                    .textContentType(creating ? .newPassword : .password)
                    .focused($focus, equals: .password)
                    .field(focused: focus == .password)
                    .onSubmit(submit)
                if resetting == nil, !creating {
                    Button("Forgot password?") { resetting = .asking; error = nil; focus = .email }
                        .buttonStyle(.plain)
                        .font(.system(size: 12))
                        .foregroundStyle(Palette.actionText)
                        .frame(minHeight: 24)
                        .padding(.top, 2)
                }
                }

                Group {
                    if let error {
                        Label(error, systemImage: "exclamationmark.circle")
                            .font(.system(size: 12))
                            .foregroundStyle(Palette.onErrorContainer)
                            .transition(.opacity)
                    }
                }
                .frame(minHeight: 18, alignment: .topLeading)
                .onChange(of: error) { _, text in if let text { AccessibilityNotification.Announcement(text).post() } }
                .padding(.top, 8)

                Button(action: submit) {
                    HStack(spacing: 8) {
                        if working { ProgressView().controlSize(.small).tint(Palette.onLink) }
                        Text(resetting == .asking ? "Email me a code" : resetting == .entering ? "Set password and sign in" : creating ? "Create account" : "Sign in")
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(.primary)
                .keyboardShortcut(.defaultAction)
                .disabled(working || email.isEmpty || (resetting != .asking && password.isEmpty) || (resetting == .entering && code.isEmpty))
                .padding(.top, 6)

                if resetting != nil {
                    Button("Back to sign in") { resetting = nil; error = nil; code = "" }
                        .buttonStyle(.plain)
                        .font(.system(size: 13))
                        .foregroundStyle(Palette.actionText)
                        .frame(maxWidth: .infinity, minHeight: 24)
                        .padding(.top, 16)
                } else {
                HStack(spacing: 4) {
                    Text(creating ? "Have an account?" : "New to Monty?").foregroundStyle(Palette.onSurfaceVariant)
                    Button(creating ? "Sign in" : "Create an account") {
                        creating.toggle()
                        error = nil
                        focus = .email
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(Palette.actionText)
                    .frame(minHeight: 24)
                }
                .font(.system(size: 13))
                .frame(maxWidth: .infinity)
                .padding(.top, 16)
                }
            }
            .frame(width: 340)
            .card(padding: 28)
            .shadow(color: .black.opacity(0.04), radius: 12, y: 4)

            if let reason = app.signedOutReason {
                Label(reason, systemImage: "info.circle")
                    .font(.system(size: 12))
                    .foregroundStyle(Palette.onSurfaceVariant)
                    .padding(.top, 14)
                    .frame(width: 396)
            }
            Spacer(minLength: 40)
            if app.serverURL != AppModel.defaultServer {
                Text("Using a custom server: \(app.serverURL.host() ?? app.serverURL.absoluteString). Change it in Settings (⌘,).")
                    .font(.system(size: 12))
                    .foregroundStyle(Palette.onSurfaceVariant)
                    .padding(.bottom, 14)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Palette.surface)
        .onAppear { focus = .email }
        .animation(.easeOut(duration: 0.15), value: error)
        .animation(.easeOut(duration: 0.15), value: creating)
    }

    private func submitReset(_ stage: Reset) {
        guard !working, !email.isEmpty else { return }
        if stage == .entering, password.count < 8 { error = "Use at least 8 characters for your password."; return }
        working = true
        error = nil
        Task {
            do {
                if stage == .asking {
                    try await app.requestPasswordReset(email: email)
                    resetting = .entering
                    password = ""
                    focus = .code
                } else {
                    try await app.resetPassword(email: email, code: code, password: password)
                    UserDefaults.standard.set(true, forKey: "hasSignedIn")
                }
            } catch {
                self.error = error.localizedDescription
            }
            working = false
        }
    }

    private func promise(_ icon: String, _ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: icon).font(.system(size: 11)).frame(width: 14).foregroundStyle(Palette.onSurfaceVariant).accessibilityHidden(true)
            Text(text).font(.system(size: 13)).fixedSize(horizontal: false, vertical: true)
        }
    }

    private func label(_ text: String) -> some View {
        Text(text).font(.system(size: 12, weight: .medium)).padding(.bottom, 5)
    }

    private func submit() {
        if let resetting { return submitReset(resetting) }
        guard !working, !email.isEmpty, !password.isEmpty else { return }
        if creating, password.count < 8 { error = "Use at least 8 characters for your password."; return }
        working = true
        error = nil
        Task {
            do {
                if creating {
                    try await app.signUp(email: email, password: password)
                } else {
                    try await app.signIn(email: email, password: password)
                }
                UserDefaults.standard.set(true, forKey: "hasSignedIn")
            } catch {
                self.error = error.localizedDescription
                focus = .password
            }
            working = false
        }
    }
}
