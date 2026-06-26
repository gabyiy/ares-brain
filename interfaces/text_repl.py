from core.intent_router import IntentRouter


def main():
    router = IntentRouter()
    awake = False

    print("ARES text mode ready.")
    print("Say 'hello ares' to wake me.")
    print("Say 'goodbye ares' to exit.")

    while True:
        try:
            user = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nARES: Goodbye Gabi.")
            break

        low = user.lower()

        if low in ("hello ares", "hi ares", "hey ares"):
            awake = True
            print("ARES:", router.handle(user))
            continue

        if low in ("goodbye", "goodbye ares", "exit", "quit"):
            print("ARES:", router.handle(user))
            break

        if not awake:
            print("ARES: I am sleeping. Say 'hello ares' to wake me.")
            continue

        try:
            print("ARES:", router.handle(user))
        except Exception as e:
            print("ARES: Error:", e)


if __name__ == "__main__":
    main()
