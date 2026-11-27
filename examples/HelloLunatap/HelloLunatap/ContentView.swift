// Copyright 2026 The Lunatap Authors
// Licensed under the Apache License, Version 2.0. See LICENSE in the repository root.

import SwiftUI

/// A small app for trying Lunatap: a counter, a text field and a second screen.
struct ContentView: View {
    @State private var taps = 0
    @State private var name = ""

    var body: some View {
        NavigationStack {
            Form {
                Section("Counter") {
                    Text("Tapped \(taps) times")
                        .accessibilityIdentifier("tapCount")
                    Button("Tap me") { taps += 1 }
                        .accessibilityIdentifier("tapButton")
                }
                Section("Greeting") {
                    TextField("Your name", text: $name)
                        .accessibilityIdentifier("nameField")
                    Text(name.isEmpty ? "Hello!" : "Hello, \(name)!")
                        .accessibilityIdentifier("greeting")
                }
                Section {
                    NavigationLink("About") { AboutView() }
                }
            }
            .navigationTitle("Hello Lunatap")
        }
    }
}

struct AboutView: View {
    var body: some View {
        Text("Built and launched by lunatap build.")
            .padding()
            .navigationTitle("About")
    }
}
