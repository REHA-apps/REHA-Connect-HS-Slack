# Connector SDK Documentation

The Connector SDK provides a platform-agnostic framework for building HubSpot integrations. It decouples business logic from platform-specific delivery mechanisms (Slack, WhatsApp, etc.).

## Core Components

### 1. UnifiedContext
Standardizes payload data across platforms.
- `platform`: The messaging provider (SLACK, WHATSAPP, etc.).
- `user_id`: The ID of the interacting user.
- `workspace_id`: The internal workspace identifier.
- `trigger_id`: For modal/dialog windows.
- `action_id`: The identifier for the clicked button or menu item.

### 2. UIAdapter (Protocol)
Defines the contract for rendering UI elements. Every platform must implement this protocol.
- `send_card()`: Renders a `UnifiedCard`.
- `open_modal()`: Opens a dialog or view.
- `update_modal()`: Updates an existing view.
- `show_loading()`: Shows a transient loading state.

### 3. BaseInteractionHandler
The abstract base for all domain handlers.
- Automatically routes actions via the `@interaction_handler` decorator.
- Provides access to `self.ui` (the adapter), `self.hubspot`, and `self.ai`.

## Adding a New Platform (e.g., WhatsApp)

1. **Implement UIAdapter**:
   ```python
   class WhatsAppUIAdapter(UIAdapter):
       async def send_card(self, context, card, ...):
           # Convert UnifiedCard to WhatsApp Template messages
           pass
   ```

2. **Wrap InteractionHandler**:
   ```python
   class WhatsAppHandler(InteractionHandler):
       # Inherits SDK logic, uses WhatsAppUIAdapter
   ```

3. **Register in InteractionRegistry**:
   Update `InteractionRegistry` to inject the new adapter when the provider is WhatsApp.

## Best Practices
- **Never** use platform-specific libraries (e.g., `slack_sdk`) inside domain handlers. Use `self.ui` instead.
- Use `UnifiedCard` for all rich record displays.
- Normalize incoming payloads into `UnifiedContext` as early as possible.
