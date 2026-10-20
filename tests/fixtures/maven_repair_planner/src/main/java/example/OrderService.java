package example;

public class OrderService {
    private final Calculator calculator = new Calculator();

    public int calculate(int price) {
        return calculator.multiply(price, 2);
    }
}
