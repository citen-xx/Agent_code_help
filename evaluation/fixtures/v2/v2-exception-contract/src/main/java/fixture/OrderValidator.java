package fixture;

public class OrderValidator {
    public void validate(int quantity) {
        if (quantity <= 0) {
            throw new IllegalArgumentException("quantity");
        }
    }
}
