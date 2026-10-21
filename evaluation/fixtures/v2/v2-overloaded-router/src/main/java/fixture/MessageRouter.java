package fixture;

public class MessageRouter {
    public String route(String value) {
        return "text:" + value;
    }

    public String route(Integer value) {
        return "text:" + value;
    }
}
