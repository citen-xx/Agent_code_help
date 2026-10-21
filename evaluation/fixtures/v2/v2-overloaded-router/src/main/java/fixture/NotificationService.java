package fixture;

public class NotificationService {
    private final MessageRouter router = new MessageRouter();
    public String notifyText(String value) { return router.route(value); }
    public String notifyNumber(Integer value) { return router.route(value); }
}
