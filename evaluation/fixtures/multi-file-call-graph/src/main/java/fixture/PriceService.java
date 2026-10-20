package fixture;

public class PriceService {
    private final Calculator calculator = new Calculator();

    public int doublePrice(int price) {
        return calculator.multiply(price, 2);
    }
}
