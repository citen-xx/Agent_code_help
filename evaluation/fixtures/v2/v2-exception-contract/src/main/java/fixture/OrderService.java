package fixture;

public class OrderService {
    private final OrderValidator validator = new OrderValidator();

    public boolean submit(int quantity) {
        try {
            validator.validate(quantity);
            return true;
        } catch (RuntimeException ignored) {
            return false;
        }
    }
}
